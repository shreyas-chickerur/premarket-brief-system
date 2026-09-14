"""Materialise Drive files without routing their bytes through the caller.

Stage 0's bootstrap has to read every `journal-*.json`, `fills-cache-*.json`
and `splits-cache-*.json` file in the Drive folder before positions can be
derived. The obvious way -- one `download_file_content` per file -- returns
each payload INLINE to the caller, so the only way to get it onto local disk
is to pass its full base64 back out again. That write-back, not the download
round trip, is the dominant cost, and it scales with the BYTES of stored
state rather than the number of files. By 14 September 2026 that state was
~299 KB across 23 files and five consecutive sessions had aborted at Stage 0
without ever completing the read (10, 11 and 14 September, plus two watchdog
retries), each one re-attempting the same bootstrap and failing the same way.

That was a deadlock rather than a slow day: `state-bundle-*.json` exists to
reduce Stage 0 to ONE read, but the first bundle can only be written by a run
that completes the bootstrap read it replaces, and no run had.

`search_files` with `snippetVerbosity="MAX_ALLOWED"` breaks it. One call
returns every matching file's FULL text in a single response; a response that
large exceeds the harness's inline tool-output limit and is auto-saved to a
file on local disk, which is the existing, documented convention for oversized
`get_equity_orders` pages. The bytes reach disk without passing through the
caller at all, so the cost stops scaling with the size of the stored state.

The one wrinkle is that snippets arrive markdown-escaped -- a backslash is
inserted before punctuation such as `_`, `[` and `]`, and each original
newline is rendered as two spaces, a newline, and a space. `unescape_snippet`
reverses exactly that, and only that: a backslash introducing a genuine JSON
escape (`\\"`, `\\\\`, `\\n`, `\\uXXXX`, ...) is left alone, because stripping it
would corrupt any string value that legitimately contains a quote or a
backslash. Whitespace is left as-is: JSON ignores it outside string literals,
and pretty-printed JSON never breaks a line inside one.

This module does not decide anything. It is transport only -- every fold,
every reconciliation, and every block-severity Stage 0 check runs afterwards
on exactly the same `{"title", "content"}` shape the per-file download path
produced, and stays exactly as strict.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

__all__ = ["JSON_ESCAPES", "unescape_snippet", "files_from_listing",
           "titles_matching"]

# The characters that may legitimately follow a backslash inside JSON. A
# backslash before anything else in a snippet was added by the markdown
# escaper and has to come back out.
JSON_ESCAPES = '"\\/bfnrtu'

_ESCAPED = re.compile(r"\\(.)", re.DOTALL)


def unescape_snippet(text: str) -> str:
    """Reverse Drive's markdown escaping of a file's content snippet.

    Strips the backslash from `\\_`, `\\[`, `\\-`, `\\$` and friends, and
    leaves genuine JSON escapes untouched so a string value containing a
    quote or a backslash survives the round trip intact.
    """
    return _ESCAPED.sub(
        lambda m: m.group(0) if m.group(1) in JSON_ESCAPES else m.group(1),
        text,
    )


def files_from_listing(listing: Any) -> list[dict]:
    """Turn a `search_files` response into the `{"title", "content"}` pairs
    every `ledger.fold_*` function already takes.

    `listing` may be the parsed response or the path to the auto-saved file
    the harness wrote it to. Files whose snippet is missing are skipped
    rather than yielded empty -- an empty `content` folds as an unreadable
    file, and a transport gap must not be mistaken for a corrupt journal.
    """
    if isinstance(listing, (str, bytes)):
        with open(listing, encoding="utf-8") as fh:
            listing = json.load(fh)
    out: list[dict] = []
    for f in listing.get("files", []):
        snippet = f.get("contentSnippet")
        if not snippet:
            continue
        out.append({"title": f.get("title", ""),
                    "content": unescape_snippet(snippet)})
    out.sort(key=lambda d: d["title"])
    return out


def titles_matching(files: Iterable[dict], prefix: str) -> list[dict]:
    """The subset of `files` whose title starts with `prefix` -- the dated
    families (`journal-`, `fills-cache-`, `splits-cache-`, ...) arrive mixed
    together when one listing covers several of them."""
    return [f for f in files if str(f.get("title", "")).startswith(prefix)]
