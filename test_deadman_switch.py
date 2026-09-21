"""The dead-man's switch is a Google Apps Script (`deadman_switch.gs`) that
runs in the user's own Google account, so it cannot be executed here. Its one
piece of logic, `decide`, is pure and is exercised under node when node exists.
The Gmail/trigger glue is small and is checked by reading it, not by test."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

H = 3_600_000
START = 1_000_000_000_000  # midnight, today, in the script's time zone
NOW = START + 9 * H + 15 * 60_000  # 09:15


def _decide(weekday, briefs, alerted, now=NOW):
    script = (
        "const {decide}=require('./deadman_switch.gs');"
        f"console.log(JSON.stringify(decide({now},{START},{weekday},{json.dumps(briefs)},{json.dumps(alerted)})));"
    )
    out = subprocess.run([NODE, "-e", script], cwd=REPO, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_a_brief_from_today_means_ok():
    assert _decide(3, [START + 7 * H], False) == "ok"


def test_no_brief_at_all_alerts():
    assert _decide(3, [], False) == "alert"


def test_only_yesterdays_brief_alerts():
    assert _decide(3, [START - 2 * H], False) == "alert"


def test_a_message_dated_after_now_does_not_count():
    assert _decide(3, [NOW + H], False) == "alert"


def test_does_not_alert_twice_the_same_day():
    assert _decide(3, [], True) == "already-alerted"


def test_a_brief_wins_even_if_an_alert_was_already_sent():
    assert _decide(3, [START + 8 * H], True) == "ok"


@pytest.mark.parametrize("weekday", [6, 7])
def test_weekends_are_skipped(weekday):
    assert _decide(weekday, [], False) == "skip-weekend"
