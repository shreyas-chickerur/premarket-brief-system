/**
 * Dead-man's switch for the Pre-Market Brief System.
 *
 * Runs in YOUR Google account (Google Apps Script), so it depends on nothing
 * this project's Claude sessions depend on: not the routines, not the weekly
 * usage cap, not the Drive/Robinhood connectors, and no third-party service.
 * Every weekday morning it looks for that day's brief in your mailbox; if none
 * has arrived by the check time it emails you an alert.
 *
 * Why this exists: on 17 September 2026 both the 06:20 routine and the 08:20
 * watchdog died within seconds (shared weekly usage cap) before writing
 * anything, and nothing could notice, because everything that could notice was
 * one of the things that died.
 *
 * The signal is the brief itself. The system sends one every weekday --
 * including a short "market closed" brief on holidays -- with a subject like
 *   "[DRY RUN] Pre-Market Brief 2026-09-21 -- 1 idea, 0 orders, degraded".
 *
 * SETUP (once, ~3 minutes):
 *   1. script.google.com > New project. Paste this whole file over Code.gs.
 *   2. Project Settings > set the time zone to America/Chicago (or leave the
 *      TIMEZONE constant below to do the work; the trigger uses it explicitly).
 *   3. Select the function `setup` and press Run. Approve the Gmail
 *      permissions when asked (it only reads subjects/dates and sends you mail).
 *   4. Optional: run `testAlert` to confirm you receive the alert email.
 * `setup` creates a daily trigger for 09:15 Central. Weekends are skipped.
 */

var TIMEZONE = 'America/Chicago';
var CHECK_HOUR = 9;
var SUBJECT_TERM = 'Pre-Market Brief';
var ALERT_PREFIX = 'ALERT: no daily brief arrived';

/**
 * Pure decision, no Google services -- unit tested under node.
 *   nowMs            current time (ms)
 *   startOfTodayMs   midnight at the start of today in TIMEZONE (ms)
 *   weekday          1 = Monday ... 7 = Sunday, in TIMEZONE
 *   briefTimesMs     internal dates (ms) of every message whose subject
 *                    contains SUBJECT_TERM and is not one of our own alerts
 *   alertedToday     true if an alert for today was already sent
 * Returns 'skip-weekend' | 'ok' | 'already-alerted' | 'alert'.
 */
function decide(nowMs, startOfTodayMs, weekday, briefTimesMs, alertedToday) {
  if (weekday >= 6) return 'skip-weekend';
  for (var i = 0; i < briefTimesMs.length; i++) {
    if (briefTimesMs[i] >= startOfTodayMs && briefTimesMs[i] <= nowMs) return 'ok';
  }
  return alertedToday ? 'already-alerted' : 'alert';
}

function checkBrief() {
  var now = new Date();
  var ymd = Utilities.formatDate(now, TIMEZONE, 'yyyy-MM-dd');
  var weekday = parseInt(Utilities.formatDate(now, TIMEZONE, 'u'), 10);
  var startOfToday = new Date(Utilities.formatDate(now, TIMEZONE, "yyyy-MM-dd'T'00:00:00XXX"));

  var briefTimes = [];
  GmailApp.search('subject:"' + SUBJECT_TERM + '" newer_than:2d', 0, 50).forEach(function (t) {
    t.getMessages().forEach(function (m) {
      if (m.getSubject().indexOf(ALERT_PREFIX) === -1) briefTimes.push(m.getDate().getTime());
    });
  });

  var alerted = GmailApp.search('subject:"' + ALERT_PREFIX + ' ' + ymd + '"', 0, 1).length > 0;
  var verdict = decide(now.getTime(), startOfToday.getTime(), weekday, briefTimes, alerted);
  Logger.log(ymd + ': ' + verdict);
  if (verdict !== 'alert') return verdict;

  var me = Session.getEffectiveUser().getEmail();
  GmailApp.sendEmail(me, ALERT_PREFIX + ' ' + ymd,
    'No "' + SUBJECT_TERM + '" email has reached this mailbox by ' + CHECK_HOUR + ':15 Central on ' + ymd + '.\n\n' +
    'The 06:20 routine and the 08:20 watchdog both normally produce one, so neither did. Most likely causes, in order:\n' +
    '  1. Claude usage credits / the weekly usage cap are exhausted (this is what happened on 17 Sep 2026).\n' +
    '  2. The routines are paused or failed to start: claude.ai/code/routines\n' +
    '  3. A connector (Gmail, Google Drive, Robinhood, Alpha Vantage) lost authorisation.\n\n' +
    'Nothing trades on its own while this is happening (the system is in DRY RUN), so the cost of a missed day is a missed brief, not a bad order.');
  return verdict;
}

/** Creates the daily 09:15 Central trigger. Safe to run again. */
function setup() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'checkBrief') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('checkBrief').timeBased().everyDays(1).atHour(CHECK_HOUR)
    .nearMinute(15).inTimezone(TIMEZONE).create();
  Logger.log('Trigger created: checkBrief daily at ' + CHECK_HOUR + ':15 ' + TIMEZONE);
}

/** Sends the alert email right now, so you can see what it looks like. */
function testAlert() {
  var me = Session.getEffectiveUser().getEmail();
  GmailApp.sendEmail(me, ALERT_PREFIX + ' TEST',
    'This is a test of the Pre-Market Brief dead-man\'s switch. If you can read this, alerts reach you.');
}

if (typeof module !== 'undefined') module.exports = { decide: decide };
