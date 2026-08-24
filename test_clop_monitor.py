import ast
import contextlib
import dataclasses
import http.client
import inspect
import io
import json
import tempfile
import textwrap
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

import clop_monitor
from goods import Stockpiles
from clop_monitor import (
    DEFAULT_WAV_PATH,
    AlertCategorySettings,
    ArchivedThreadError,
    ClopClient,
    FourChanPost,
    MonitorError,
    Notifier,
    Snapshot,
    build_alerts,
    check_and_notify,
    main,
    parse_report_rows,
    surviving_report_lines,
    settings_startup_message,
    load_settings,
    load_env_file,
    parse_fourchan_comment,
    parse_fourchan_thread_url,
    load_snapshot,
    parse_latest_news,
    parse_latest_report,
    parse_pending_counts,
    save_snapshot,
)


def shipped_example():
    """The tracked settings.example.json, parsed."""
    return json.loads(Path("settings.example.json").read_text(encoding="utf-8-sig"))


def report_raises_no_alert(message, patterns):
    """Whether the report raises no alert, which is now "no line of it survived".

    Named for what it asks rather than for the production function it replaced, which was
    deleted when reports gained lines: a name that survives only in tests sends the next
    reader looking for a symbol the monitor no longer has.
    """
    return not surviving_report_lines(message, patterns)


def shipped_report_patterns():
    """Every shipped reports.ignore pattern, as if the reader had switched them all on."""
    return [entry.lstrip("#").strip() for entry in shipped_example()["reports"]["ignore"]]


AUTHENTICATED_HEADER = """
<nav>
  <a href="messages.php">Messages <span class="badge"> (2)</span></a>
  <a href="myalliance.php">My Alliance<span class="badge"> (7)</span></a>
  <a href="logout.php">Logout</a>
</nav>
"""


class ParserTests(unittest.TestCase):
    def test_pending_counts(self):
        self.assertEqual(parse_pending_counts(AUTHENTICATED_HEADER), (2, 7))

    def test_empty_pending_counts(self):
        html = '<a href="messages.php">Messages <span></span></a>'
        self.assertEqual(parse_pending_counts(html), (0, 0))

    def test_latest_news_uses_only_top_timestamped_row(self):
        html = """
        <div>Server time: 2026-08-17 00:00:00</div>
        <table>
          <tr><td>A <a href="viewuser.php?id=1">user</a> joined.</td><td>2026-08-17 01:02:03</td></tr>
          <tr><td>Second item</td><td>2026-08-16 04:05:06</td></tr>
          <tr><th>Not</th><th>news</th></tr>
        </table>
        """
        self.assertEqual(parse_latest_news(html), ("A user joined.", "2026-08-17 01:02:03"))

    def test_latest_report_uses_only_top_timestamped_row(self):
        html = """
        <table>
          <tr><td>You gained <strong>3 Pies</strong>.<br>Done.</td><td>2026-08-17 08:02:33</td></tr>
          <tr><td>Older report</td><td>2026-08-16 04:05:06</td></tr>
        </table>
        """
        self.assertEqual(
            parse_latest_report(html),
            # The <br> stays a line break: reports are judged and shown a line at a time.
            ("You gained 3 Pies.\nDone.", "2026-08-17 08:02:33"),
        )

    def test_fourchan_comment_html_becomes_plain_text(self):
        self.assertEqual(
            parse_fourchan_comment(
                '<span class="quote">&gt;quoted</span><br>Hello <b>everypony</b>'
            ),
            ">quoted Hello everypony",
        )

    def test_fourchan_thread_url_becomes_api_url(self):
        thread = parse_fourchan_thread_url(
            "https://boards.4chan.org/mlp/thread/43437288/clop-the-great-reset"
        )
        self.assertEqual(thread.board, "mlp")
        self.assertEqual(thread.thread_id, 43437288)
        self.assertEqual(thread.api_url, "https://a.4cdn.org/mlp/thread/43437288.json")

    def test_non_fourchan_thread_url_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "boards.4chan.org"):
            parse_fourchan_thread_url("https://example.com/mlp/thread/43437288")

    def test_latest_fourchan_post_is_parsed_from_api_json(self):
        payload = {
            "posts": [
                {"no": 1, "time": 100, "name": "Anonymous", "com": "Old"},
                {
                    "no": 2,
                    "time": 200,
                    "name": "Pony",
                    "com": "Newest<br>message",
                },
            ]
        }

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.close()

        class FakeOpener:
            def open(self, request, timeout):
                del request, timeout
                return FakeResponse(json.dumps(payload).encode("utf-8"))

        thread = parse_fourchan_thread_url("https://boards.4chan.org/mlp/thread/123/example")
        client = ClopClient("https://4clop.org", "user", "password", fourchan_thread=thread)
        client.opener = FakeOpener()
        self.assertEqual(
            client._latest_fourchan_post(),
            FourChanPost(thread.page_url, 2, 200, "Pony", "Newest message"),
        )

    def test_archived_fourchan_thread_is_rejected(self):
        payload = {
            "posts": [
                {
                    "no": 1,
                    "time": 100,
                    "name": "Anonymous",
                    "com": "Archived",
                    "archived": 1,
                    "archived_on": 200,
                }
            ]
        }

        class FakeResponse(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self.close()

        class FakeOpener:
            def open(self, request, timeout):
                del request, timeout
                return FakeResponse(json.dumps(payload).encode("utf-8"))

        thread = parse_fourchan_thread_url("https://boards.4chan.org/mlp/thread/123/example")
        client = ClopClient("https://4clop.org", "", "", fourchan_thread=thread)
        client.opener = FakeOpener()
        with self.assertRaisesRegex(ArchivedThreadError, "Set fourchan.thread_url to null"):
            client._latest_fourchan_post()

    def test_preflight_post_is_reused_without_second_request(self):
        thread = parse_fourchan_thread_url("https://boards.4chan.org/mlp/thread/123/example")
        initial = FourChanPost(thread.page_url, 2, 200, "Pony", "Latest")

        class FailingOpener:
            def open(self, request, timeout):
                del request, timeout
                raise AssertionError("API should not be requested while the preflight post is cached")

        client = ClopClient(
            "https://4clop.org",
            "",
            "",
            fourchan_thread=thread,
            initial_fourchan_post=initial,
        )
        client.opener = FailingOpener()
        self.assertEqual(client._latest_fourchan_post(), initial)


class StateTests(unittest.TestCase):
    def test_stale_alliance_badge_is_revalidated_through_fresh_login(self):
        stale_navigation = AUTHENTICATED_HEADER.replace("(7)", "(3)").replace("(2)", "")
        fresh_navigation = AUTHENTICATED_HEADER.replace("(7)", "").replace("(2)", "")
        calls = []
        client = ClopClient("https://4clop.org", "user", "password")

        def fake_open(path, form=None):
            calls.append((path, form))
            if path == "index.php":
                return stale_navigation
            if path == "login.php":
                return fresh_navigation
            if path == "news.php?page=1":
                return fresh_navigation + "<h3>News</h3>No news yet."
            if path == "reports.php":
                return fresh_navigation + "<h3>Reports</h3><table></table>"
            raise AssertionError(f"Unexpected path: {path}")

        client._open = fake_open
        snapshot = client.snapshot()
        self.assertEqual((snapshot.user_messages, snapshot.alliance_messages), (0, 0))
        self.assertEqual([path for path, _ in calls].count("login.php"), 1)

    def test_genuine_alliance_badge_survives_fresh_login(self):
        navigation = AUTHENTICATED_HEADER.replace("(7)", "(3)").replace("(2)", "")
        client = ClopClient("https://4clop.org", "user", "password")

        def fake_open(path, form=None):
            del form
            if path in {"index.php", "login.php"}:
                return navigation
            if path == "news.php?page=1":
                return navigation + "<h3>News</h3>No news yet."
            if path == "reports.php":
                return navigation + "<h3>Reports</h3><table></table>"
            raise AssertionError(f"Unexpected path: {path}")

        client._open = fake_open
        self.assertEqual(client.snapshot().alliance_messages, 3)

    def test_state_round_trip(self):
        post = FourChanPost(
            "https://boards.4chan.org/mlp/thread/123",
            456,
            789,
            "Anonymous",
            "Newest post",
        )
        snapshot = Snapshot(
            3,
            4,
            ("News", "2026-08-17 01:02:03"),
            post,
            ("Report", "2026-08-17 08:02:33"),
            True,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "state.json"
            save_snapshot(path, snapshot)
            self.assertEqual(load_snapshot(path), snapshot)

    def test_invalid_saved_counts_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "not integers"):
            Snapshot.from_json({"user_messages": "many", "alliance_messages": 0})

    def test_original_news_list_state_is_migrated_to_top_entry(self):
        snapshot = Snapshot.from_json(
            {
                "user_messages": 0,
                "alliance_messages": 0,
                "news": [
                    {"message": "Newest", "posted": "2026-08-17 01:02:03"},
                    {"message": "Older", "posted": "2026-08-16 01:02:03"},
                ],
            }
        )
        self.assertEqual(snapshot.latest_news, ("Newest", "2026-08-17 01:02:03"))

    def test_initial_snapshot_only_alerts_for_pending_messages(self):
        snapshot = Snapshot(1, 0, ("Old news", "2026-08-17 01:02:03"))
        self.assertEqual(
            build_alerts(None, snapshot),
            ["1 unread user message(s) pending"],
        )

    def test_pending_messages_alert_when_count_is_unchanged(self):
        previous = Snapshot(1, 2, ("Same news", "2026-08-17 01:02:03"))
        current = Snapshot(1, 2, ("Same news", "2026-08-17 01:02:03"))
        self.assertEqual(
            build_alerts(previous, current),
            [
                "1 unread user message(s) pending",
                "2 unread alliance message(s) pending",
            ],
        )

    def test_alert_categories_can_be_disabled_independently(self):
        previous = Snapshot(0, 0, ("Old", "2026-08-16 01:02:03"))
        current = Snapshot(1, 2, ("New", "2026-08-17 01:02:03"))
        settings = AlertCategorySettings(
            user_messages=False,
            alliance_messages=True,
            news=False,
            reports=False,
        )
        self.assertEqual(
            build_alerts(previous, current, settings),
            ["2 unread alliance message(s) pending"],
        )

    def test_pending_messages_and_news_change_are_described(self):
        previous = Snapshot(0, 1, ("Old news", "2026-08-16 01:02:03"))
        current = Snapshot(2, 0, ("New news", "2026-08-17 01:02:03"))
        alerts = build_alerts(previous, current)
        self.assertEqual(
            alerts,
            [
                "2 unread user message(s) pending",
                "Newest news changed (2026-08-17 01:02:03): New news",
            ],
        )

    def test_new_report_alerts_after_report_baseline(self):
        previous = Snapshot(
            0,
            0,
            None,
            latest_report=("Old report", "2026-08-16 08:02:33"),
            reports_checked=True,
        )
        current = Snapshot(
            0,
            0,
            None,
            latest_report=("New report", "2026-08-17 08:02:33"),
            reports_checked=True,
        )
        alerts = build_alerts(previous, current)
        self.assertEqual(len(alerts), 1)
        self.assertIn("New CLOP report (2026-08-17 08:02:33): New report", alerts[0])
        self.assertIn("https://4clop.org/reports.php", alerts[0])

    def test_first_report_after_known_empty_feed_alerts(self):
        previous = Snapshot(0, 0, None, latest_report=None, reports_checked=True)
        current = Snapshot(
            0,
            0,
            None,
            latest_report=("First report", "2026-08-17 08:02:33"),
            reports_checked=True,
        )
        self.assertEqual(len(build_alerts(previous, current)), 1)

    def test_existing_report_silently_establishes_migrated_baseline(self):
        previous = Snapshot(0, 0, None)
        current = Snapshot(
            0,
            0,
            None,
            latest_report=("Existing report", "2026-08-17 08:02:33"),
            reports_checked=True,
        )
        self.assertEqual(build_alerts(previous, current), [])

    def test_report_alert_can_be_disabled(self):
        previous = Snapshot(
            0,
            0,
            None,
            latest_report=("Old", "2026-08-16 08:02:33"),
            reports_checked=True,
        )
        current = Snapshot(
            0,
            0,
            None,
            latest_report=("New", "2026-08-17 08:02:33"),
            reports_checked=True,
        )
        settings = AlertCategorySettings(reports=False)
        self.assertEqual(build_alerts(previous, current, settings), [])

    def test_new_fourchan_post_alerts_after_baseline(self):
        thread_url = "https://boards.4chan.org/mlp/thread/123"
        previous = Snapshot(
            0,
            0,
            None,
            FourChanPost(thread_url, 1, 100, "Anonymous", "Old"),
        )
        current = Snapshot(
            0,
            0,
            None,
            FourChanPost(thread_url, 2, 200, "Pony", "New post"),
        )
        alerts = build_alerts(previous, current)
        self.assertEqual(len(alerts), 1)
        self.assertIn("New 4chan /mlp/ post #2 by Pony", alerts[0])
        self.assertIn("New post", alerts[0])
        self.assertIn(f"{thread_url}#p2", alerts[0])

    def test_first_or_different_fourchan_thread_establishes_baseline(self):
        old = FourChanPost("https://boards.4chan.org/mlp/thread/111", 1, 100, "A", "Old")
        new = FourChanPost("https://boards.4chan.org/mlp/thread/222", 2, 200, "B", "New")
        self.assertEqual(build_alerts(None, Snapshot(0, 0, None, new)), [])
        self.assertEqual(build_alerts(Snapshot(0, 0, None, old), Snapshot(0, 0, None, new)), [])

    def test_counts_are_refetched_after_a_dismissal_but_markers_are_not(self):
        """The refresh exists to re-read counts; it must not step over unseen news.

        The news in ``after_dismissal`` landed while the dialog was open, so it has never
        been shown. Adopting it as the baseline here would lose it for good.
        """
        detected = Snapshot(1, 0, ("Detected", "2026-08-17 01:02:03"))
        after_dismissal = Snapshot(0, 0, ("After dismissal", "2026-08-17 01:03:03"))

        class FakeClient:
            def __init__(self):
                self.snapshots = [detected, after_dismissal]
                self.calls = 0

            def snapshot(self, include_market=True):
                del include_market
                result = self.snapshots[self.calls]
                self.calls += 1
                return result

        class BlockingNotifier:
            def __init__(self):
                self.messages = []

            def notify(self, message):
                self.messages.append(message)
                return True

        client = FakeClient()
        notifier = BlockingNotifier()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            current, paused = check_and_notify(client, detected, notifier, path)
            self.assertTrue(paused)
            self.assertEqual(client.calls, 2)
            self.assertEqual(current.user_messages, 0)
            self.assertEqual(current.latest_news, ("Detected", "2026-08-17 01:02:03"))
            self.assertEqual(load_snapshot(path), current)
        self.assertEqual(notifier.messages, ["1 unread user message(s) pending"])
        # The next poll still has the news that arrived while the dialog was open.
        self.assertEqual(len(build_alerts(current, after_dismissal)), 1)

    def test_file_cache_can_be_disabled(self):
        observed = Snapshot(0, 0, ("News", "2026-08-17 01:02:03"))

        class FakeClient:
            def snapshot(self, include_market=True):
                del include_market
                return observed

        class FailingNotifier:
            def notify(self, message):
                raise AssertionError(f"Unexpected alert: {message}")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            current, paused = check_and_notify(
                FakeClient(),
                None,
                FailingNotifier(),
                path,
                persist_state=False,
            )
            self.assertEqual(current, observed)
            self.assertFalse(paused)
            self.assertFalse(path.exists())


class SettingsTests(unittest.TestCase):
    def test_missing_env_file_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            self.assertEqual(load_env_file(path), {})

    def test_env_file_loads_credentials_and_ignores_comments(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# local credentials\n"
                "CLOP_USERNAME='Example User'\n"
                'CLOP_PASSWORD="secret=value"\n'
                "export CLOP_WEBHOOK_URL=https://example.invalid/hook\n",
                encoding="utf-8",
            )
            values = load_env_file(path)
        self.assertEqual(values["CLOP_USERNAME"], "Example User")
        self.assertEqual(values["CLOP_PASSWORD"], "secret=value")
        self.assertEqual(values["CLOP_WEBHOOK_URL"], "https://example.invalid/hook")

    def test_invalid_env_file_entry_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("not-an-assignment", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "expected NAME=VALUE"):
                load_env_file(path)

    def test_empty_settings_use_requested_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{}", encoding="utf-8")
            settings = load_settings(path)
        self.assertTrue(settings.alerts.user_messages)
        self.assertTrue(settings.alerts.alliance_messages)
        self.assertTrue(settings.alerts.news)
        self.assertTrue(settings.alerts.reports)
        self.assertTrue(settings.cache.persist_to_file)
        self.assertEqual(settings.sound.wav_path, DEFAULT_WAV_PATH)
        self.assertFalse(settings.sound.loop_while_popup_open)
        self.assertEqual(settings.sound.repeat_interval_seconds, 10)
        self.assertIsNone(settings.fourchan_thread)

    def test_file_cache_setting_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"cache": {"persist_to_file": False}}),
                encoding="utf-8",
            )
            settings = load_settings(path)
        self.assertFalse(settings.cache.persist_to_file)

    def test_relative_wav_and_loop_settings_are_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.json"
            wav_path = Path(directory) / "alert.wav"
            wav_path.touch()
            settings_path.write_text(
                json.dumps(
                    {
                        "sound": {
                            "wav_path": "alert.wav",
                            "loop_while_popup_open": True,
                            "repeat_interval_seconds": 3.5,
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = load_settings(settings_path)
        self.assertEqual(settings.sound.wav_path, wav_path.resolve())
        self.assertTrue(settings.sound.loop_while_popup_open)
        self.assertEqual(settings.sound.repeat_interval_seconds, 3.5)

    def test_missing_wav_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"sound":{"wav_path":"missing.wav"}}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "was not found"):
                load_settings(path)

    def test_fourchan_thread_setting_is_optional_and_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "fourchan": {
                            "thread_url": "https://boards.4chan.org/mlp/thread/43437288/title"
                        }
                    }
                ),
                encoding="utf-8",
            )
            settings = load_settings(path)
        self.assertIsNotNone(settings.fourchan_thread)
        self.assertEqual(settings.fourchan_thread.thread_id, 43437288)


class SettingsDefaultsTests(unittest.TestCase):
    """A settings file is optional: an absent file or key follows the built-in default."""

    def test_missing_settings_file_uses_built_in_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = load_settings(Path(directory) / "settings.json")
        self.assertFalse(settings.file_found)
        self.assertTrue(settings.alerts.news)
        self.assertTrue(settings.cache.persist_to_file)
        self.assertEqual(settings.sound.wav_path, DEFAULT_WAV_PATH)
        self.assertIsNone(settings.fourchan_thread)

    def test_startup_message_reports_a_missing_settings_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            message = settings_startup_message(load_settings(path), path)
        self.assertIn("no settings file", message)
        self.assertIn(str(path), message)
        self.assertIn("4chan thread monitoring off", message)

    def test_startup_message_names_only_the_omitted_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "alerts": {
                            "user_messages": True,
                            "alliance_messages": True,
                            "news": True,
                            "reports": True,
                            "market_orders": True,
                        },
                        "sound": {
                            "wav_path": None,
                            "loop_while_popup_open": False,
                        },
                        "fourchan": {"thread_url": None},
                    }
                ),
                encoding="utf-8",
            )
            message = settings_startup_message(load_settings(path), path)
        self.assertEqual(
            message,
            "Settings: using defaults for sound.repeat_interval_seconds, "
            "cache.persist_to_file, reports.ignore, market.goods.",
        )

    def test_complete_settings_file_reports_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "alerts": {
                            "user_messages": True,
                            "alliance_messages": True,
                            "news": True,
                            "reports": True,
                            "market_orders": True,
                        },
                        "sound": {
                            "wav_path": None,
                            "loop_while_popup_open": False,
                            "repeat_interval_seconds": 10,
                        },
                        "cache": {"persist_to_file": True},
                        "reports": {"ignore": []},
                        "market": {"goods": {}},
                        "fourchan": {"thread_url": None},
                    }
                ),
                encoding="utf-8",
            )
            message = settings_startup_message(load_settings(path), path)
        self.assertIsNone(message)

    def test_explicit_null_wav_path_uses_the_system_sound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text('{"sound":{"wav_path":null}}', encoding="utf-8")
            settings = load_settings(path)
        self.assertIsNone(settings.sound.wav_path)

    def test_report_ignore_patterns_are_loaded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"reports": {"ignore": ["Burn Oil", "Distribute Pies"]}}),
                encoding="utf-8",
            )
            settings = load_settings(path)
        self.assertEqual(settings.alerts.report_ignore, ("Burn Oil", "Distribute Pies"))

    def test_a_commented_out_pattern_is_not_applied(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"reports": {"ignore": ["# Burn Oil", "  #Distribute Pies", "Change in Satisfaction:"]}}),
                encoding="utf-8",
            )
            settings = load_settings(path)
        self.assertEqual(settings.alerts.report_ignore, ("Change in Satisfaction:",))

    def test_a_hash_after_the_start_stays_part_of_the_pattern(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"reports": {"ignore": ["Report #% filed"]}}), encoding="utf-8"
            )
            settings = load_settings(path)
        self.assertEqual(settings.alerts.report_ignore, ("Report #% filed",))
        self.assertTrue(report_raises_no_alert("Report #42 filed", settings.alerts.report_ignore))

    def test_every_pattern_commented_out_ignores_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"reports": {"ignore": ["# Burn Oil", "# Distribute Pies"]}}),
                encoding="utf-8",
            )
            settings = load_settings(path)
        self.assertEqual(settings.alerts.report_ignore, ())
        self.assertNotIn("reports.ignore", settings.defaults_used)

    def test_report_ignore_defaults_to_nothing_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{}", encoding="utf-8")
            settings = load_settings(path)
        self.assertEqual(settings.alerts.report_ignore, ())
        self.assertIn("reports.ignore", settings.defaults_used)

    def test_report_ignore_must_be_a_list_of_non_empty_strings(self):
        for value in [{"ignore": "Burn Oil"}, {"ignore": [""]}, {"ignore": [7]}]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.json"
                path.write_text(json.dumps({"reports": value}), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "reports.ignore"):
                    load_settings(path)

    def test_absent_bundled_wav_falls_back_to_the_system_sound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("{}", encoding="utf-8")
            with mock.patch.object(
                clop_monitor, "DEFAULT_WAV_PATH", Path(directory) / "absent.wav"
            ):
                settings = load_settings(path)
        self.assertIsNone(settings.sound.wav_path)


#: Each shipped choice with a real-shaped report it silences. The first four are logical report
#: selectors: one tick, all Build-named actions, exactly Burn Oil, and exactly Distribute Pies.
#: The rest are backwards-compatible line patterns for standalone report families.
IGNORABLE_REPORTS = [
    (
        "Tick",
        "Show Details\nHide Details\nYou gained 5 Oil from your 1 Basic Oil Well.\n"
        "Change in Satisfaction: -2\nChange in SE Relation: +1\nChange in NLR Relation: 0",
    ),
    (
        "Action: Build %",
        "You spent 20 Machinery Parts.\nYou paid 50,000 bits.\n"
        "Build Advanced Factory completed successfully.",
    ),
    (
        "Action: Burn Oil",
        "You spent 5 Oil.\nYou gained 5 Energy.\n"
        "Your population's satisfaction has dwindled due to your 1 Burn Oil. (-5)\n"
        "Burn Oil completed successfully.",
    ),
    (
        "Action: Distribute Pies",
        "You spent 1 Pies.\n"
        "Your population's satisfaction has improved due to your 1 Distribute Pies. (+2)\n"
        "Distribute Pies completed successfully.",
    ),
    (
        "You bought % from % for % bits.",
        "You bought 50 Apples from Luna Sueno for 55,000 bits.",
    ),
    (
        "You transferred % to % for % bits.",
        "You transferred 20 Oil to Buenos Mares for 40,000 bits.",
    ),
    (
        "This nation paid % bits and",
        "This nation paid 5,000 bits and Buenos Mares received 4,850 bits.",
    ),
    ("You dealt away % %.", "You dealt away 5 Oil."),
    ("You accepted a deal with", "You accepted a deal with Luna Sueno."),
    (
        "You have created the military force",
        "You have created the military force First Cavalry.",
    ),
]

#: Every routine sentence the game can write to `reports`, one line per source template, read
#: from the game at D:\Koan\clop\clop. This is the coverage target: the shipped patterns must
#: silence all of it, and no shipped pattern may be droppable while it still does, which is
#: what stops the list drifting back into a pattern per wording.
ROUTINE_REPORTS = [
    # backend_actions.php:240 / :189 / :232 / :235 / :216 - a finished action, whatever its name
    "Dig Basic Copper Mine completed successfully.",
    "Build Advanced Factory completed successfully.",
    "Upgrade to Gasoline Combustion Facility completed successfully.",
    "Receive Factory Aid completed successfully.",
    "You spent 20 Machinery Parts.",
    "You paid 50,000 bits.",
    "You gained 50,000 bits.",
    "You gained 5 Oil.",
    # frequent.php:327 / :335 - the tick's production and consumption
    "You gained 5 Oil from your 1 Basic Oil Well.",
    "Your 3 Basic Factory used up 5 Oil.",
    # frequent.php:506-637 - the government and economy upkeep bills, all six wordings
    "Your Democracy used 20 gasoline.",
    "Your Democracy used 2 vehicle parts.",
    "Your Independence used 40 gasoline.",
    "Your decentralized government used 50 gasoline.",
    "Your machinery of Repression used 10 gasoline.",
    "Your Authoritarian government used 10 gasoline and 3 machinery parts.",
    "Your machinery of Oppression used 10 gasoline and 5 machinery parts.",
    "Your State Controllers drank 6 cider.",
    "Your Free Marketeers drank 6 coffee.",
    # frequent.php:397-472 - military force upkeep, UTC hours 0 and 12
    "Your First Cavalry used up 12 apples.",
    # allfunctions.php:157 / :162 / :167 - relations, four wordings and two caps
    "Your relationship with the Solar Empire has improved due to your 1 Basic Oil Well. (+3)",
    "Your relationship with the New Lunar Republic has worsened due to your 3 Basic Factory. (-2)",
    "Your relationship with the Solar Empire can't get any better, despite the effects of "
    "your 1 Basic Oil Well.",
    "Your relationship with the Solar Empire can't get any worse- your 1 Basic Oil Well "
    "sure tried, though!",
    # allfunctions.php:190 / :195 - satisfaction, the same four wordings and its cap
    "Your population's satisfaction has dwindled due to your 3 Basic Factory. (-4)",
    "Your population can't be any more satisfied, despite the effects of your 3 Basic Factory.",
    # frequent.php:776-795 - the wrapper and the three totals
    "Show Details",
    "Hide Details",
    "Change in Satisfaction: -2",
    "Change in SE Relation: +1",
    "Change in NLR Relation: 0",
    "You're ascending; your relationships with the Solar Empire and New Lunar Republic "
    "can only go down.",
    # frequent.php:666-752 - the satisfaction and relationship caps, and the floor on hate
    "You hit the Democracy satisfaction cap of 1500. (-40)",
    "You hit the satisfaction cap of 1000. (-40)",
    "You hit the Solar Empire relationship cap of 1000. (-12)",
    "Even for the Solar Empire, there are limits to hate. (+5)",
    # frequent.php:152-236 - what decays on its own
    "A satisfied population is hard to keep. (-5 sat)",
    "A good friend is hard to keep; you lose 2 relationship with the Solar Empire.",
    "A bad enemy forgets eventually; you gain 2 relationship with the Solar Empire.",
    # frequent.php:347 / :365 - the siphons, singular and plural
    "As you have more than 50,000 Oil, 300 was siphoned off.",
    "As you have more than 1,000 Tanks, 5 were siphoned off.",
    # frequent.php:355 / :248 / :254 - the environment healing, and each Empire's jealousy
    "Some of the environmental damage has been repaired. (5 sat)",
    "The Solar Empire doesn't like your good relations with the New Lunar Republic. (-3)",
    "The New Lunar Republic doesn't like your good relations with the Solar Empire. (-3)",
    # backend_marketplace.php:207 / backend_transfer.php:60 / :151 - trades you made
    "You bought 50 Apples from Luna Sueno for 55,000 bits.",
    "You transferred 20 Oil to Buenos Mares for 40,000 bits.",
    "This nation paid 5,000 bits and Buenos Mares received 4,850 bits.",
    # backend_deals.php:229 / :250 / :314 - a deal you accepted
    "You accepted a deal with Luna Sueno.",
    "You dealt away 5 Oil.",
    "You dealt away 5,000 bits.",
    # backend_createforces.php:99
    "You have created the military force First Cavalry.",
]

#: Every line from the catalogue's "Notable" list, with the wording the game writes. No
#: shipped pattern may silence any of these: an unedited monitor must not be able to hide a
#: warning, and this list is the whole safety property of judging reports line by line.
NOTABLE_REPORTS = [
    "You couldn't pay the upkeep for your First Cavalry and it's gone!",
    "Your Barracks scattered to the four winds!",
    "Your First Cavalry lost 3 size!",
    "Your Second Cavalry (size 10) were hit by Sombra's Shadow Guard (size 12) "
    "for 40 damage (3 hits)",
    # frequent.php:1296/:1302 writes the attacker's half too, and it is the only combat line
    # that reports something going right, which is exactly why it needs guarding: it reads
    # more like bookkeeping than the other three.
    "Your First Cavalry (size 10) hit Sombra's Shadow Guard (size 12) "
    "for 40 damage (3 hits)",
    "Your satisfaction is below the minimum - your ponies are revolting! (You gain 30 sat "
    "among the rest of your nation as the subversives stop participating in it.)",
    "The Solar Empire hates you enough to send an airstrike and you don't have enough "
    "support from its opponent! (Wishing to avoid a protracted war, it has reduced its "
    "hate for you by 50.)",
    "The Solar Empire has attacked you for daring to ascend! (-50)",
    "You don't have enough Oil to run your 3 Basic Factory!",
    "Too many Basic Oil Wells cause environmental damage! (-5 sat)",
    "You lose 4 satisfaction for having 4 disabled buildings.",
    "You lost 5 satisfaction for not having any buildings!",
    "You lost 30 satisfaction for having a military of total size 300.",
    "You lose 10 sat for having an empire of 3 nations.",
    "Your Democracy lacks the gasoline and vehicle parts to function properly! (-20 sat)",
    "Your government lacks the gasoline to function properly! (-50 sat)",
    "Your economy lacks the cider to function properly! (-25 sat, unable to make deals)",
    "Your deal with Luna Sueno was rejected.",
    "Your deal with Luna Sueno was accepted.",
    "Your deal with Luna Sueno was canceled.",
    "You received 5 Oil as part of your deal.",
    "You sold 10 Copper to quaity kirenese merch and ice and made 9,000 bits.",
    "This nation received 20 Apples from Buenos Mares.",
    "This nation received 4,850 bits from Buenos Mares.",
    "You have completed the forbidden research, and the facility has been automatically "
    "dismantled. A new Major Action is available to you.",
]

#: A quiet two-hourly tick's detail lines: production, consumption, the relation and
#: satisfaction effects, an upkeep bill, a cap, and a siphon. Between them the shipped
#: patterns have to leave nothing.
ROUTINE_TICK_DETAILS = [
    "You gained 5 Oil from your 1 Basic Oil Well.",
    "Your 3 Basic Factory used up 5 Oil.",
    "You gained 12 Machinery Parts from your 3 Basic Factory.",
    "Your relationship with the Solar Empire has improved due to your 1 Basic Oil Well. (+3)",
    "Your population's satisfaction has dwindled due to your 3 Basic Factory. (-4)",
    "Your Democracy used 20 gasoline.",
    "Your Democracy used 2 vehicle parts.",
    "You hit the Democracy satisfaction cap of 1500. (-40)",
    "A satisfied population is hard to keep. (-5 sat)",
    "As you have more than 50,000 Oil, 300 was siphoned off.",
    "Your First Cavalry used up 12 apples.",
]

#: Every action name in the shipped recipes table that does NOT begin "Build ", so the old
#: Build-only pattern silently missed it. Taken from clop/tables with data.sql.
NON_BUILD_COMPLETIONS = [
    "Dig Basic Copper Mine",
    "Plow Basic Apple Orchard",
    "Burn Oil",
    "Distribute Apples",
    "Distribute Pies",
    "Distribute Money",
    "Upgrade Oil Well",
    "Upgrade Copper Mine",
    "Upgrade Apple Orchard",
    "Upgrade Plastics Factory",
    "Upgrade to Gasoline Combustion Facility",
    "Plow Coffee Farm",
    "Dig Gem Mine",
    "Dig Tungsten Mine",
    "Manufacture Precision Parts",
    "Manufacture Composites",
    "Ship Oil to the Solar Empire",
    "Ship Oil to the New Lunar Republic",
    "Ship Tungsten to the Solar Empire",
    "Ship Tungsten to the New Lunar Republic",
    "Smuggle Drugs into the SE",
    "Smuggle Drugs into the NLR",
    "Drug Farm",
    "Receive Factory Aid",
]
WORTH_ALERTING = "Your nation was attacked by Sombra and lost 3 Tanks."


def tick_report(detail_lines, satisfaction="-2"):
    """One two-hourly tick report, shaped the way cron/frequent.php:786-799 writes it.

    Copied structurally rather than paraphrased: the Show/Hide wrapper is two <div>s, the
    detail lines are joined with <br/>, and nothing but a </div> separates the last detail
    line from "Change in Satisfaction:". Those three facts are what the line split has to
    survive, so a tidied-up fixture would test the wrong page.
    """
    details = "<br/>\n".join(detail_lines)
    return (
        '<div id="r1" class="report-showbutton">'
        "<a href=\"javascript:;\" onclick=\"document.getElementById('r1').style.display = 'none';\n"
        "document.getElementById('r1x').style.display = 'block';\">Show Details</a></div>\n"
        '\t<div id="r1x" class="report-details">'
        "<a href=\"javascript:;\" onclick=\"document.getElementById('r1').style.display = 'block';\n"
        "document.getElementById('r1x').style.display = 'none';\">Hide Details</a><br/>\n"
        f"{details}\n"
        "\t</div>\n"
        "\t<b>\n"
        f"\tChange in Satisfaction: {satisfaction}<br/>\n"
        "\tChange in SE Relation: +1<br/>\n"
        "\tChange in NLR Relation: 0<br/>\n"
        "\t</b>"
    )


def action_report(*infos):
    """One finished action's report: backend_actions.php:246 joins its lines with <br/>."""
    return "<br/>".join(infos)


def reports_page(cells, timestamps=None):
    """The reports.php table, newest first, as reports.php renders it."""
    stamps = timestamps or [f"2026-08-17 08:{index:02d}:00" for index in range(len(cells), 0, -1)]
    rows = "".join(
        f"<tr><td>{cell}</td><td>{stamp}</td></tr>" for cell, stamp in zip(cells, stamps)
    )
    return f"<h3>Reports</h3><table><tr><th>Report</th><th>When</th></tr>{rows}</table>"


class ReportIgnorePatternTests(unittest.TestCase):
    """Patterns match anywhere in a report; % stands for any run of characters."""

    def test_each_example_pattern_ignores_its_own_report(self):
        for pattern, message in IGNORABLE_REPORTS:
            with self.subTest(pattern=pattern):
                self.assertTrue(report_raises_no_alert(message, [pattern]))

    def test_no_example_pattern_ignores_a_report_worth_seeing(self):
        patterns = [pattern for pattern, _ in IGNORABLE_REPORTS]
        self.assertFalse(report_raises_no_alert(WORTH_ALERTING, patterns))

    def test_a_buy_pattern_does_not_ignore_a_sale(self):
        sale = "You sold 10 Copper to quaity kirenese merch and ice and made 9,000 bits."
        self.assertFalse(report_raises_no_alert(sale, ["You bought % from % for % bits."]))

    def test_matching_ignores_case(self):
        self.assertTrue(report_raises_no_alert("BURN OIL consumed 5 barrels", ["burn oil"]))

    def test_a_wildcard_spans_any_text_including_none(self):
        self.assertTrue(
            report_raises_no_alert(
                "Build Really Very Large Ovipositor Factory completed successfully.",
                ["Build % completed successfully."],
            )
        )
        self.assertTrue(
            report_raises_no_alert("Build  completed successfully.", ["Build % completed successfully."])
        )

    def test_a_wildcard_may_start_or_end_a_pattern(self):
        self.assertTrue(report_raises_no_alert("You made 9,000 bits.", ["% bits."]))

    def test_no_patterns_ignores_nothing(self):
        self.assertFalse(report_raises_no_alert(WORTH_ALERTING, []))

    def test_shipped_choices_are_all_present(self):
        value = shipped_example()
        self.assertEqual(
            [entry.lstrip("#").strip() for entry in value["reports"]["ignore"]],
            [pattern for pattern, _ in IGNORABLE_REPORTS],
        )
        self.assertNotIn("_ignore_examples", value["reports"])

    def test_uncommenting_a_shipped_pattern_switches_it_on(self):
        value = shipped_example()
        value["reports"]["ignore"] = ["Action: Burn Oil"]
        # The bundled WAV is reached by a path relative to the settings file, which a temp
        # directory has no copy of; the sound is irrelevant here.
        value["sound"]["wav_path"] = None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            settings = load_settings(path)
        self.assertEqual(settings.alerts.report_ignore, ("Action: Burn Oil",))
        self.assertTrue(
            report_raises_no_alert(
                "You spent 5 Oil.\nYou gained 5 Energy.\n"
                "Your population's satisfaction has dwindled due to your 1 Burn Oil. (-5)\n"
                "Burn Oil completed successfully.",
                settings.alerts.report_ignore,
            )
        )


class CompletionPatternTests(unittest.TestCase):
    """Every finished action reports "<action name> completed successfully.".

    The name comes straight from the recipes table, so a Build-only pattern silences the 38
    actions that happen to be named "Build ..." and misses the other 24.
    """

    def report(self, action):
        return f"{action} completed successfully."

    def test_the_catch_all_covers_every_non_build_action(self):
        for action in NON_BUILD_COMPLETIONS:
            with self.subTest(action=action):
                self.assertTrue(
                    report_raises_no_alert(self.report(action), ["% completed successfully."])
                )

    def test_a_build_only_pattern_misses_every_one_of_them(self):
        for action in NON_BUILD_COMPLETIONS:
            with self.subTest(action=action):
                self.assertFalse(
                    report_raises_no_alert(self.report(action), ["Build % completed successfully."])
                )

    def test_the_catch_all_still_covers_the_build_actions(self):
        self.assertTrue(
            report_raises_no_alert(
                self.report("Build Advanced Factory"), ["% completed successfully."]
            )
        )

    def test_a_completion_is_silenced_alongside_the_lines_it_ships_with(self):
        # backend_actions.php:246 joins every line of one action into a single report, so one
        # matching pattern silences the paid/gained/satisfaction lines with it.
        whole = (
            "You spent 20 Machinery Parts. You paid 50,000 bits. "
            "Build Advanced Factory completed successfully."
        )
        self.assertTrue(report_raises_no_alert(whole, ["% completed successfully."]))

    def test_the_catch_all_leaves_reports_worth_seeing_alone(self):
        for message in (
            WORTH_ALERTING,
            "Your Barracks scattered to the four winds!",
            "You couldn't pay the upkeep for your First Cavalry and it's gone!",
            "You have completed the forbidden research, and the facility has been "
            "automatically dismantled. A new Major Action is available to you.",
        ):
            with self.subTest(message=message[:40]):
                self.assertFalse(report_raises_no_alert(message, ["% completed successfully."]))


class LogicalReportSelectorTests(unittest.TestCase):
    """Tick and Action entries represent game reports, not their incidental sentences."""

    def test_build_selector_silences_the_whole_multiline_build_report(self):
        report = (
            "You spent 20 Machinery Parts.\n"
            "You paid 50,000 bits.\n"
            "Your population's satisfaction has improved due to your 1 Build Bakery. (+2)\n"
            "Build Bakery completed successfully."
        )
        self.assertEqual(surviving_report_lines(report, ["Action: Build %"]), [])

    def test_exact_action_choices_are_selective(self):
        burn = (
            "You spent 5 Oil.\nYou gained 5 Energy.\n"
            "Your population's satisfaction has dwindled due to your 1 Burn Oil. (-5)\n"
            "Burn Oil completed successfully."
        )
        pies = (
            "You spent 1 Pies.\n"
            "Your population's satisfaction has improved due to your 1 Distribute Pies. (+2)\n"
            "Distribute Pies completed successfully."
        )
        self.assertEqual(surviving_report_lines(burn, ["Action: Burn Oil"]), [])
        self.assertEqual(surviving_report_lines(pies, ["Action: Distribute Pies"]), [])
        self.assertIn(
            "Burn Oil completed successfully.",
            surviving_report_lines(burn, ["Action: Distribute Pies"]),
        )

    def test_selected_action_does_not_hide_a_warning_in_a_merged_cell(self):
        warning = "You couldn't pay the upkeep for your First Cavalry and it's gone!"
        report = (
            "You spent 5 Oil.\nBurn Oil completed successfully.\n" + warning
        )
        self.assertEqual(surviving_report_lines(report, ["Action: Burn Oil"]), [warning])

    def test_tick_word_is_not_a_broad_substring_pattern(self):
        self.assertEqual(
            surviving_report_lines("The clock is ticking normally.", ["Tick"]),
            ["The clock is ticking normally."],
        )


class ReportScanTests(unittest.TestCase):
    """Every report since the last one seen is judged, not just the newest."""

    @staticmethod
    def _snapshot(rows):
        return Snapshot(
            0,
            0,
            None,
            latest_report=rows[0] if rows else None,
            reports_checked=True,
            report_rows=tuple(rows),
        )

    def test_every_report_since_the_last_is_alerted_newest_first(self):
        previous = self._snapshot([("Older", "2026-08-17 08:00:00")])
        current = self._snapshot(
            [
                ("Third", "2026-08-17 08:03:00"),
                ("Second", "2026-08-17 08:02:00"),
                ("First", "2026-08-17 08:01:00"),
                ("Older", "2026-08-17 08:00:00"),
            ]
        )
        alerts = build_alerts(previous, current)
        self.assertEqual(len(alerts), 3)
        self.assertIn("Third", alerts[0])
        self.assertIn("Second", alerts[1])
        self.assertIn("First", alerts[2])

    def test_ignored_reports_are_dropped_from_a_mixed_batch(self):
        ignore = [pattern for pattern, _ in IGNORABLE_REPORTS]
        previous = self._snapshot([("Older", "2026-08-17 08:00:00")])
        current = self._snapshot(
            [
                (WORTH_ALERTING, "2026-08-17 08:03:00"),
                (IGNORABLE_REPORTS[0][1], "2026-08-17 08:02:00"),
                (IGNORABLE_REPORTS[2][1], "2026-08-17 08:01:00"),
                ("Older", "2026-08-17 08:00:00"),
            ]
        )
        alerts = build_alerts(previous, current, AlertCategorySettings(report_ignore=tuple(ignore)))
        self.assertEqual(len(alerts), 1)
        self.assertIn(WORTH_ALERTING, alerts[0])

    def test_a_batch_of_only_ignored_reports_alerts_nothing(self):
        ignore = [pattern for pattern, _ in IGNORABLE_REPORTS]
        previous = self._snapshot([("Older", "2026-08-17 08:00:00")])
        current = self._snapshot(
            [
                (message, f"2026-08-17 08:{index + 1:02d}:00")
                for index, (_, message) in enumerate(IGNORABLE_REPORTS)
            ]
            + [("Older", "2026-08-17 08:00:00")]
        )
        alerts = build_alerts(previous, current, AlertCategorySettings(report_ignore=tuple(ignore)))
        self.assertEqual(alerts, [])

    def test_reports_sharing_one_timestamp_are_all_alerted(self):
        previous = self._snapshot([("Older", "2026-08-17 08:00:00")])
        current = self._snapshot(
            [
                ("Same second B", "2026-08-17 08:01:00"),
                ("Same second A", "2026-08-17 08:01:00"),
                ("Older", "2026-08-17 08:00:00"),
            ]
        )
        alerts = build_alerts(previous, current)
        self.assertEqual(len(alerts), 2)

    def test_a_marker_no_longer_on_the_page_falls_back_to_its_timestamp(self):
        previous = self._snapshot([("Scrolled off", "2026-08-17 08:00:00")])
        current = self._snapshot(
            [
                ("Newer", "2026-08-17 08:02:00"),
                ("Also newer", "2026-08-17 08:01:00"),
                ("As old as the marker", "2026-08-17 08:00:00"),
            ]
        )
        alerts = build_alerts(previous, current)
        self.assertEqual(len(alerts), 2)

    def test_a_long_absence_alerts_every_report_without_a_cap(self):
        previous = self._snapshot([("Older", "2026-08-17 07:00:00")])
        current = self._snapshot(
            [(f"Report {index}", f"2026-08-17 08:{index:02d}:00") for index in range(40, 0, -1)]
            + [("Older", "2026-08-17 07:00:00")]
        )
        self.assertEqual(len(build_alerts(previous, current)), 40)


class ReportRowParsingTests(unittest.TestCase):
    def test_every_timestamped_row_is_returned_newest_first(self):
        html = """
        <h3>Reports</h3>
        <table>
          <tr><th>Report</th><th>When</th></tr>
          <tr><td>Newest report</td><td>2026-08-17 08:02:33</td></tr>
          <tr><td>Older report</td><td>2026-08-16 04:05:06</td></tr>
        </table>
        """
        self.assertEqual(
            parse_report_rows(html),
            [
                ("Newest report", "2026-08-17 08:02:33"),
                ("Older report", "2026-08-16 04:05:06"),
            ],
        )


class PerLineReportJudgingTests(unittest.TestCase):
    """reports.ignore judges each line of a report, and the alert shows the survivors."""

    @staticmethod
    def alerts(message, patterns, posted="2026-08-17 08:01:00"):
        previous = Snapshot(
            0,
            0,
            None,
            latest_report=("Older", "2026-08-17 08:00:00"),
            reports_checked=True,
            report_rows=(("Older", "2026-08-17 08:00:00"),),
        )
        current = Snapshot(
            0,
            0,
            None,
            latest_report=(message, posted),
            reports_checked=True,
            report_rows=((message, posted), ("Older", "2026-08-17 08:00:00")),
        )
        return build_alerts(
            previous, current, AlertCategorySettings(report_ignore=tuple(patterns))
        )

    def test_a_report_whose_every_line_matches_is_silent(self):
        message = (
            "You spent 20 Machinery Parts.\n"
            "You paid 50,000 bits.\n"
            "Build Advanced Factory completed successfully."
        )
        self.assertEqual(
            self.alerts(
                message,
                ["You spent % %.", "You paid % bits.", "% completed successfully."],
            ),
            [],
        )

    def test_only_the_surviving_line_is_alerted(self):
        message = (
            "You gained 5 Oil from your 1 Basic Oil Well.\n"
            "You couldn't pay the upkeep for your First Cavalry and it's gone!\n"
            "Change in Satisfaction: -2"
        )
        alerts = self.alerts(message, ["You gained % %.", "Change in Satisfaction:"])
        self.assertEqual(len(alerts), 1)
        self.assertIn(
            "New CLOP report (2026-08-17 08:01:00): "
            "You couldn't pay the upkeep for your First Cavalry and it's gone!",
            alerts[0],
        )
        self.assertNotIn("You gained 5 Oil", alerts[0])
        self.assertNotIn("Change in Satisfaction", alerts[0])

    def test_several_survivors_are_alerted_one_per_line(self):
        message = "Routine.\nFirst warning!\nRoutine.\nSecond warning!"
        alerts = self.alerts(message, ["Routine."])
        self.assertIn("First warning!\nSecond warning!", alerts[0])

    def test_no_patterns_alerts_the_whole_report(self):
        alerts = self.alerts("Line one.\nLine two.", [])
        self.assertEqual(len(alerts), 1)
        self.assertIn("Line one.\nLine two.", alerts[0])

    def test_a_pattern_cannot_reach_across_a_line_break(self):
        # Whole-report matching would have found this, because the report was one string.
        self.assertEqual(
            len(self.alerts("You paid\n50,000 bits.", ["You paid 50,000 bits."])), 1
        )

    def test_the_length_cap_applies_to_the_survivors(self):
        message = "\n".join(["Routine."] * 200 + ["Warning!"])
        alerts = self.alerts(message, ["Routine."])
        self.assertIn(": Warning!\n", alerts[0])
        self.assertNotIn("...", alerts[0])


class ShippedPatternSafetyTests(unittest.TestCase):
    """What the shipped patterns must and must not reach, read off the report catalogue."""

    def test_every_shipped_pattern_silences_the_line_it_is_for(self):
        for pattern, message in IGNORABLE_REPORTS:
            with self.subTest(pattern=pattern):
                self.assertEqual(surviving_report_lines(message, [pattern]), [])

    def test_no_shipped_pattern_silences_a_notable_line(self):
        patterns = shipped_report_patterns()
        for message in NOTABLE_REPORTS:
            with self.subTest(message=message[:50]):
                self.assertEqual(surviving_report_lines(message, patterns), [message])

    def test_the_shipped_set_is_exactly_the_patterns_the_fixture_documents(self):
        self.assertEqual(shipped_report_patterns(), [pattern for pattern, _ in IGNORABLE_REPORTS])

    def test_tick_is_one_choice_covering_every_routine_tick_family_from_the_source(self):
        # ROUTINE_REPORTS[8:44] is the complete frequent.php tick corpus above: production,
        # consumption, upkeep, effects, wrapper, caps, decay, siphons, repair and jealousy.
        report = tick_report(ROUTINE_REPORTS[8:44])
        page_lines = parse_report_rows(reports_page([report]))[0][0]
        self.assertEqual(surviving_report_lines(page_lines, ["Tick"]), [])

    def test_action_choices_are_not_permutations_of_the_completion_sentence(self):
        self.assertEqual(
            shipped_report_patterns()[:4],
            ["Tick", "Action: Build %", "Action: Burn Oil", "Action: Distribute Pies"],
        )


class ShippedPatternsOnRealReportsTests(unittest.TestCase):
    """The whole point: a quiet tick goes silent, a tick with a warning shows the warning."""

    def alerts_for(self, cells):
        page = reports_page(cells)
        rows = parse_report_rows(page)
        previous = Snapshot(
            0, 0, None, latest_report=None, reports_checked=True, report_rows=()
        )
        current = Snapshot(
            0,
            0,
            None,
            latest_report=rows[0],
            reports_checked=True,
            report_rows=tuple(rows),
        )
        return build_alerts(
            previous,
            current,
            AlertCategorySettings(report_ignore=tuple(shipped_report_patterns())),
        )

    def test_a_routine_tick_is_completely_silent(self):
        self.assertEqual(self.alerts_for([tick_report(ROUTINE_TICK_DETAILS)]), [])

    def test_a_tick_that_lost_a_force_alerts_with_that_line_alone(self):
        lost = "You couldn't pay the upkeep for your First Cavalry and it's gone!"
        alerts = self.alerts_for([tick_report(ROUTINE_TICK_DETAILS + [lost])])
        self.assertEqual(len(alerts), 1)
        self.assertIn(f": {lost}\n", alerts[0])
        for routine in ROUTINE_TICK_DETAILS:
            self.assertNotIn(routine, alerts[0])

    def test_every_notable_line_survives_the_tick_it_arrives_in(self):
        for notable in NOTABLE_REPORTS:
            with self.subTest(message=notable[:50]):
                # Last, because that is the position the game's own report format makes
                # riskiest: only a </div> separates it from "Change in Satisfaction:".
                alerts = self.alerts_for([tick_report(ROUTINE_TICK_DETAILS + [notable])])
                self.assertEqual(len(alerts), 1)
                self.assertIn(f": {notable}\n", alerts[0])

    def test_a_finished_action_is_completely_silent(self):
        report = action_report(
            "You spent 20 Machinery Parts.",
            "You paid 50,000 bits.",
            "Build Advanced Factory completed successfully.",
        )
        self.assertEqual(self.alerts_for([report]), [])

    def test_a_merged_block_alerts_with_only_the_notable_report_s_lines(self):
        # backend_reports.php:5-12 joins reports sharing a timestamp into one cell, so a
        # routine action and a lost force can be judged together.
        merged = "<br/>".join(
            [
                action_report(
                    "You paid 50,000 bits.", "Build Advanced Factory completed successfully."
                ),
                tick_report(
                    ROUTINE_TICK_DETAILS
                    + ["You couldn't pay the upkeep for your First Cavalry and it's gone!"]
                ),
            ]
        )
        alerts = self.alerts_for([merged])
        self.assertEqual(len(alerts), 1)
        self.assertIn(
            ": You couldn't pay the upkeep for your First Cavalry and it's gone!\n", alerts[0]
        )

    def test_switched_off_as_shipped_the_whole_tick_alerts(self):
        page = reports_page([tick_report(ROUTINE_TICK_DETAILS)])
        rows = parse_report_rows(page)
        previous = Snapshot(0, 0, None, reports_checked=True, report_rows=())
        current = Snapshot(
            0, 0, None, latest_report=rows[0], reports_checked=True, report_rows=tuple(rows)
        )
        alerts = build_alerts(previous, current)
        self.assertEqual(len(alerts), 1)
        for routine in ROUTINE_TICK_DETAILS:
            self.assertIn(routine, alerts[0])


class UpgradedMarkerTests(unittest.TestCase):
    """A marker saved before reports kept their lines must not replay the reports it marks.

    The saved message is the old flattened form, so it is no longer on the page under that
    text and the identity match cannot find it. Nothing was migrated, so this is the whole
    upgrade path and it is checked rather than assumed: getting it wrong means a burst of
    stale alerts the first time an upgraded monitor polls.
    """

    FLATTENED = "Show Details Hide Details You gained 5 Oil from your 1 Basic Oil Well. Change in Satisfaction: -2"

    def saved_marker(self):
        """The marker as an older version wrote it, read back through the state file."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "user_messages": 0,
                        "alliance_messages": 0,
                        "latest_news": None,
                        "latest_report": {
                            "message": self.FLATTENED,
                            "posted": "2026-08-17 08:00:00",
                        },
                        "reports_checked": True,
                    }
                ),
                encoding="utf-8",
            )
            return load_snapshot(path)

    def current(self, rows):
        return Snapshot(
            0,
            0,
            None,
            latest_report=rows[0],
            reports_checked=True,
            report_rows=tuple(rows),
        )

    def test_the_report_the_old_marker_names_does_not_alert_again(self):
        multiline = self.FLATTENED.replace(" Hide", "\nHide").replace(
            "Details You", "Details\nYou"
        ).replace(" Change", "\nChange")
        alerts = build_alerts(
            self.saved_marker(), self.current([(multiline, "2026-08-17 08:00:00")])
        )
        self.assertEqual(alerts, [])

    def test_a_report_written_after_the_old_marker_still_alerts(self):
        alerts = build_alerts(
            self.saved_marker(),
            self.current(
                [
                    ("Newer report", "2026-08-17 08:02:00"),
                    ("Marked report, reparsed", "2026-08-17 08:00:00"),
                ]
            ),
        )
        self.assertEqual(len(alerts), 1)
        self.assertIn("Newer report", alerts[0])

    def test_a_sibling_sharing_the_markers_timestamp_is_dropped_on_upgrade(self):
        """The one report an upgrade can lose, pinned deliberately rather than by accident.

        The timestamp fallback is a strict `>`, so a different report written in the same
        second as the marker is treated as already seen. That is the safe direction — the
        alternative replays the marked report itself on every upgrade — but it is a real,
        one-off loss and it should not be discovered by surprise later.
        """
        alerts = build_alerts(
            self.saved_marker(),
            self.current(
                [
                    ("A different report, same second", "2026-08-17 08:00:00"),
                    ("Marked report, reparsed", "2026-08-17 08:00:00"),
                ]
            ),
        )
        self.assertEqual(alerts, [])


class ReportLineParsingTests(unittest.TestCase):
    """A report cell keeps the line structure the game wrote; a news cell does not."""

    def test_a_tick_report_comes_back_as_the_lines_the_page_shows(self):
        html = reports_page(
            [
                tick_report(
                    [
                        "You gained 5 Oil from your 1 Basic Oil Well.",
                        "You couldn't pay the upkeep for your First Cavalry and it's gone!",
                    ]
                )
            ]
        )
        (message, _), = parse_report_rows(html)
        self.assertEqual(
            message.split("\n"),
            [
                "Show Details",
                "Hide Details",
                "You gained 5 Oil from your 1 Basic Oil Well.",
                "You couldn't pay the upkeep for your First Cavalry and it's gone!",
                "Change in Satisfaction: -2",
                "Change in SE Relation: +1",
                "Change in NLR Relation: 0",
            ],
        )

    def test_the_last_detail_line_is_not_glued_to_the_satisfaction_line(self):
        # cron/frequent.php:786-799 puts no <br/> between the last detail line and
        # "Change in Satisfaction:" - only the </div> that closes the details block. Judged as
        # one line, the routine satisfaction pattern would silence whatever that last line was.
        html = reports_page([tick_report(["Too many Basic Oil Wells cause environmental damage! (-5 sat)"])])
        (message, _), = parse_report_rows(html)
        self.assertIn("Too many Basic Oil Wells cause environmental damage! (-5 sat)", message.split("\n"))

    def test_a_literal_newline_inside_one_message_stays_one_line(self):
        # Seven tick reports span more than one source line - frequent.php:952, :985 and
        # :1014 as two-line double-quoted strings, :1296, :1302, :1309 and :1315 as three-line
        # heredocs - so the newline is inside the sentence rather than between two of them.
        html = reports_page(
            [
                "Your satisfaction is below the minimum - your ponies are revolting!\n"
                "(You gain 30 sat among the rest of your nation as the subversives stop "
                "participating in it.)"
            ]
        )
        (message, _), = parse_report_rows(html)
        self.assertEqual(
            message,
            "Your satisfaction is below the minimum - your ponies are revolting! "
            "(You gain 30 sat among the rest of your nation as the subversives stop "
            "participating in it.)",
        )

    def test_an_inline_link_inside_a_line_does_not_break_it(self):
        # The combat report links the attacker's nation mid-sentence (frequent.php:1309).
        html = reports_page(
            [
                "Your Second Cavalry (size 10) were hit by\n"
                '<a href="viewnation.php?nation_id=5">Sombra</a>&#39;s Shadow Guard (size 12)\n'
                "for 40 damage (3 hits)"
            ]
        )
        (message, _), = parse_report_rows(html)
        self.assertEqual(
            message,
            "Your Second Cavalry (size 10) were hit by Sombra's Shadow Guard (size 12) "
            "for 40 damage (3 hits)",
        )

    def test_news_is_still_flattened_to_one_line(self):
        html = """
        <h3>News</h3>
        <table>
          <tr><td>War declared<br/>by Sombra</td><td>2026-08-17 08:02:33</td></tr>
        </table>
        """
        self.assertEqual(
            parse_latest_news(html), ("War declared by Sombra", "2026-08-17 08:02:33")
        )

    def test_a_block_tag_in_a_news_cell_now_separates_the_halves(self):
        """The one input where news text is not what the old parser produced.

        Before reports kept their lines, a block tag separated nothing and the two halves ran
        together into one word. None of the game's ten INSERT INTO news sites writes a block
        tag, so no real news line changed - but the behaviour is different and pinning it here
        stops anyone claiming the two forms are identical.
        """
        html = """
        <h3>News</h3>
        <table>
          <tr><td><div>War declared</div><div>by Sombra</div></td>
              <td>2026-08-17 08:02:33</td></tr>
        </table>
        """
        self.assertEqual(
            parse_latest_news(html), ("War declared by Sombra", "2026-08-17 08:02:33")
        )


class ReportsDuringAnAlertTests(unittest.TestCase):
    """A report that lands while the popup is open must survive the dismissal refresh."""

    def test_report_arriving_during_the_dialog_is_not_swallowed(self):
        before = Snapshot(
            2,
            0,
            None,
            latest_report=("Alerted report", "2026-08-17 08:01:00"),
            reports_checked=True,
            report_rows=(
                ("Alerted report", "2026-08-17 08:01:00"),
                ("Older", "2026-08-17 08:00:00"),
            ),
        )
        after = Snapshot(
            0,
            0,
            None,
            latest_report=("Landed during the popup", "2026-08-17 08:02:00"),
            reports_checked=True,
            report_rows=(
                ("Landed during the popup", "2026-08-17 08:02:00"),
                ("Alerted report", "2026-08-17 08:01:00"),
                ("Older", "2026-08-17 08:00:00"),
            ),
        )

        class TwoPollClient:
            def __init__(self):
                self.snapshots = [before, after]

            def snapshot(self, include_market=True):
                del include_market
                return self.snapshots.pop(0)

        class BlockingNotifier(Notifier):
            def notify(self, message):
                del message
                return True

        previous = Snapshot(
            0,
            0,
            None,
            latest_report=("Older", "2026-08-17 08:00:00"),
            reports_checked=True,
            report_rows=(("Older", "2026-08-17 08:00:00"),),
        )
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            state = Path(directory) / "state.json"
            current, paused = check_and_notify(
                TwoPollClient(), previous, BlockingNotifier(desktop=False), state
            )
            reloaded = load_snapshot(state)
        self.assertTrue(paused)
        # The counts are re-read, because dismissing the popup is when messages get read...
        self.assertEqual(current.user_messages, 0)
        # ...but the marker stays at what was alerted on, so the next poll still sees 08:02:00.
        self.assertEqual(current.latest_report, ("Alerted report", "2026-08-17 08:01:00"))
        self.assertEqual(reloaded.latest_report, ("Alerted report", "2026-08-17 08:01:00"))
        self.assertEqual(len(build_alerts(reloaded, after)), 1)


class MarkerCarryOverTests(unittest.TestCase):
    """News, report, and 4chan markers all survive the post-dismissal refresh."""

    @staticmethod
    def _check(before, after, previous):
        class TwoPollClient:
            def __init__(self):
                self.snapshots = [before, after]

            def snapshot(self, include_market=True):
                del include_market
                return self.snapshots.pop(0)

        class BlockingNotifier(Notifier):
            def notify(self, message):
                del message
                return True

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            current, _ = check_and_notify(
                TwoPollClient(),
                previous,
                BlockingNotifier(desktop=False),
                Path(directory) / "state.json",
            )
        return current

    def test_fourchan_post_arriving_during_the_dialog_is_not_swallowed(self):
        thread = "https://boards.4chan.org/mlp/thread/123"
        seen = FourChanPost(thread, 1, 100, "A", "Baseline")
        alerted = FourChanPost(thread, 2, 200, "B", "What the alert was about")
        during = FourChanPost(thread, 3, 300, "C", "Landed while the dialog was open")
        previous = Snapshot(1, 0, None, seen)
        current = self._check(
            Snapshot(1, 0, None, alerted), Snapshot(0, 0, None, during), previous
        )
        self.assertEqual(current.fourchan_post, alerted)
        # The post from during the dialog is still unseen, so it alerts on the next poll.
        self.assertEqual(len(build_alerts(current, Snapshot(0, 0, None, during))), 1)

    def test_news_arriving_during_the_dialog_is_not_swallowed(self):
        previous = Snapshot(1, 0, ("Baseline", "2026-08-17 01:00:00"))
        current = self._check(
            Snapshot(1, 0, ("Alerted", "2026-08-17 01:01:00")),
            Snapshot(0, 0, ("During the dialog", "2026-08-17 01:02:00")),
            previous,
        )
        self.assertEqual(current.latest_news, ("Alerted", "2026-08-17 01:01:00"))


class CombinedAlertTests(unittest.TestCase):
    def test_several_new_reports_arrive_as_one_notification(self):
        rows = [
            ("Third", "2026-08-17 08:03:00"),
            ("Second", "2026-08-17 08:02:00"),
            ("First", "2026-08-17 08:01:00"),
            ("Older", "2026-08-17 08:00:00"),
        ]
        previous = Snapshot(
            0, 0, None, latest_report=rows[3], reports_checked=True, report_rows=(rows[3],)
        )
        current = Snapshot(
            0, 0, None, latest_report=rows[0], reports_checked=True, report_rows=tuple(rows)
        )

        class OnePollClient:
            def snapshot(self, include_market=True):
                del include_market
                return current

        messages = []

        class RecordingNotifier(Notifier):
            def notify(self, message):
                messages.append(message)
                return False

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            check_and_notify(
                OnePollClient(),
                previous,
                RecordingNotifier(desktop=False),
                Path(directory) / "state.json",
            )
        self.assertEqual(len(messages), 1)
        for expected in ("Third", "Second", "First"):
            self.assertIn(expected, messages[0])


class FailureNotificationTests(unittest.TestCase):
    """Every failure is announced through the alert channels, never terminal-only."""

    def test_failure_uses_the_blocking_dialog_with_an_error_title(self):
        dialogs = []

        class RecordingNotifier(Notifier):
            def _desktop_notification(self, message, title="CLOP monitor", error=False):
                dialogs.append((message, title, error))
                return True

        notifier = RecordingNotifier(desktop=True)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertTrue(notifier.notify_failure("the roof is on fire"))
        message, title, error = dialogs[0]
        self.assertIn("the roof is on fire", message)
        self.assertEqual(title, "CLOP monitor problem")
        self.assertTrue(error)

    def test_failure_dialog_obeys_no_desktop_notifications(self):
        notifier = Notifier(desktop=False)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertFalse(notifier.notify_failure("the roof is on fire"))
        self.assertIn("the roof is on fire", stderr.getvalue())

    def _record_failures(self):
        """Replace the notifier so tests can read what the monitor announced.

        The "Sheet sync is OFF" startup dialog is filtered out: these tests run without
        CLOP_NATION, so it fires every time and is not what any of them is about.
        ``SheetSyncOffTests`` covers it directly.
        """
        failures = []

        class RecordingNotifier(Notifier):
            def notify(self, message):
                return False

            def notify_failure(self, message):
                if "Sheet sync is OFF" not in message:
                    failures.append(message)
                return True

        patcher = mock.patch.object(clop_monitor, "Notifier", RecordingNotifier)
        patcher.start()
        self.addCleanup(patcher.stop)
        return failures

    def _write_settings(self, directory, value):
        path = Path(directory) / "settings.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_unreadable_settings_alert_before_exit(self):
        failures = self._record_failures()
        with tempfile.TemporaryDirectory() as directory:
            unreadable = Path(directory) / "settings.json"
            unreadable.write_text("{not json", encoding="utf-8")
            code = main(
                [
                    "--settings",
                    str(unreadable),
                    "--env-file",
                    str(Path(directory) / "absent.env"),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("Could not read settings file", failures[0])
        self.assertIn("The monitor has stopped", failures[0])

    def test_missing_settings_file_starts_the_monitor_and_says_so(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(stdout):
            code = main(
                [
                    "--settings",
                    str(Path(directory) / "settings.json"),
                    "--env-file",
                    str(Path(directory) / "absent.env"),
                    "--no-desktop-notifications",
                    "--test-notification",
                ]
            )
        self.assertEqual(code, 0)
        self.assertIn("no settings file", stdout.getvalue())

    def test_archived_thread_alerts_before_exit(self):
        failures = self._record_failures()

        class ArchivedClient:
            def __init__(self, *args, **kwargs):
                del args, kwargs

            def _latest_fourchan_post(self):
                raise ArchivedThreadError("Configured 4chan thread is archived")

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            clop_monitor, "ClopClient", ArchivedClient
        ):
            settings = self._write_settings(
                directory,
                {
                    "fourchan": {
                        "thread_url": "https://boards.4chan.org/mlp/thread/43437288/title"
                    }
                },
            )
            code = main(
                [
                    "--settings",
                    str(settings),
                    "--env-file",
                    str(Path(directory) / "absent.env"),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("archived", failures[0])
        self.assertIn("The monitor has stopped", failures[0])

    def _run_with_failing_poll(self, directory, extra_args, sleep):
        """Drive main() against a client whose every poll raises."""

        class FailingClient:
            base_url = "https://4clop.org"

            def __init__(self, *args, **kwargs):
                del args, kwargs

            def login(self):
                return None

            def market_preflight(self, goods):
                del goods
                return None

            def snapshot(self, include_market=True):
                del include_market
                raise MonitorError("Could not reach https://4clop.org")

        settings = self._write_settings(directory, {"cache": {"persist_to_file": False}})
        with mock.patch.object(clop_monitor, "ClopClient", FailingClient), mock.patch.object(
            clop_monitor.time, "sleep", sleep
        ), mock.patch.dict(clop_monitor.os.environ, {"CLOP_PASSWORD": "secret"}):
            return main(
                [
                    "--settings",
                    str(settings),
                    "--env-file",
                    str(Path(directory) / "absent.env"),
                    "--username",
                    "tester",
                    "--state",
                    str(Path(directory) / "state.json"),
                ]
                + extra_args
            )

    def test_failed_poll_alerts_and_keeps_polling(self):
        failures = self._record_failures()

        def stop_after_first_sleep(seconds):
            del seconds
            raise KeyboardInterrupt

        with tempfile.TemporaryDirectory() as directory:
            code = self._run_with_failing_poll(directory, [], stop_after_first_sleep)
        self.assertEqual(code, 0)
        self.assertEqual(len(failures), 1)
        self.assertIn("Could not reach", failures[0])
        self.assertIn("still running", failures[0])

    def test_failed_single_poll_alerts_and_exits_nonzero(self):
        failures = self._record_failures()

        def unexpected_sleep(seconds):
            del seconds
            raise AssertionError("--once must not sleep")

        with tempfile.TemporaryDirectory() as directory:
            code = self._run_with_failing_poll(directory, ["--once"], unexpected_sleep)
        self.assertEqual(code, 1)
        self.assertIn("Could not reach", failures[0])
        self.assertIn("The monitor has stopped", failures[0])


class WatchedGoodTests(unittest.TestCase):
    def test_a_watched_good_defaults_to_friends_and_alliance_with_no_overrides(self):
        good = clop_monitor.WatchedGood("Machinery Parts")
        self.assertEqual(good.name, "Machinery Parts")
        self.assertTrue(good.friends)
        self.assertTrue(good.alliance)
        self.assertEqual(good.always, ())
        self.assertEqual(good.never, ())
        self.assertEqual(good.reserve, "none")
        self.assertEqual(good.reserve_amount, 0)

    def test_alert_categories_default_to_market_on_with_nothing_watched(self):
        settings = clop_monitor.AlertCategorySettings()
        self.assertTrue(settings.market_orders)
        self.assertEqual(settings.market_goods, ())


class PatternMatchTests(unittest.TestCase):
    def test_a_pattern_matches_anywhere_ignoring_case(self):
        self.assertTrue(clop_monitor.matches_any_pattern("Luna Sueno", ["luna"]))

    def test_a_wildcard_stands_for_any_run_of_characters(self):
        self.assertTrue(clop_monitor.matches_any_pattern("Big Pony Land", ["Big % Land"]))

    def test_no_patterns_match_nothing(self):
        self.assertFalse(clop_monitor.matches_any_pattern("Luna Sueno", []))


class MarketSettingsTests(unittest.TestCase):
    def load(self, market):
        value = {"sound": {"wav_path": None}, "market": market}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            return load_settings(path)

    def test_a_commented_out_good_is_not_watched(self):
        settings = self.load({"goods": {"# Machinery Parts": {}}})
        self.assertEqual(settings.alerts.market_goods, ())

    def test_uncommenting_a_good_watches_it_with_the_documented_defaults(self):
        settings = self.load({"goods": {"Machinery Parts": {}}})
        self.assertEqual(
            settings.alerts.market_goods,
            (
                clop_monitor.WatchedGood(
                    "Machinery Parts", friends=True, alliance=True, always=(), never=()
                ),
            ),
        )

    def test_every_knob_is_loaded(self):
        settings = self.load(
            {
                "goods": {
                    "Oil": {
                        "friends": False,
                        "alliance": True,
                        "always": ["Luna Sueno"],
                        "never": ["Sombra"],
                        "reserve": "ticks",
                        "reserve_amount": 4,
                    }
                }
            }
        )
        self.assertEqual(
            settings.alerts.market_goods,
            (
                clop_monitor.WatchedGood(
                    "Oil",
                    friends=False,
                    alliance=True,
                    always=("Luna Sueno",),
                    never=("Sombra",),
                    reserve="ticks",
                    reserve_amount=4,
                ),
            ),
        )

    def test_an_invalid_reserve_mode_is_rejected(self):
        for reserve in ("quantity", "Ticks", None, 1):
            with self.subTest(reserve=reserve):
                with self.assertRaisesRegex(MonitorError, "none, qty, ticks"):
                    self.load({"goods": {"Oil": {"reserve": reserve}}})

    def test_reserve_amount_must_be_a_non_negative_integer(self):
        for amount in (-1, 1.5, True, "4"):
            with self.subTest(amount=amount):
                with self.assertRaisesRegex(MonitorError, "reserve_amount"):
                    self.load({"goods": {"Oil": {"reserve_amount": amount}}})

    def test_a_commented_out_nation_name_is_dropped(self):
        settings = self.load({"goods": {"Oil": {"always": ["# Luna Sueno", "Sombra"]}}})
        self.assertEqual(settings.alerts.market_goods[0].always, ("Sombra",))

    def test_goods_keep_the_order_the_file_lists_them_in(self):
        settings = self.load({"goods": {"Oil": {}, "Apples": {}, "Copper": {}}})
        self.assertEqual(
            [good.name for good in settings.alerts.market_goods],
            ["Oil", "Apples", "Copper"],
        )

    def test_a_good_whose_value_is_not_an_object_is_rejected(self):
        with self.assertRaisesRegex(MonitorError, "Machinery Parts"):
            self.load({"goods": {"Machinery Parts": True}})

    def test_a_non_boolean_knob_is_rejected(self):
        with self.assertRaisesRegex(MonitorError, "friends"):
            self.load({"goods": {"Oil": {"friends": "yes"}}})

    def test_a_non_list_name_override_is_rejected(self):
        with self.assertRaisesRegex(MonitorError, "never"):
            self.load({"goods": {"Oil": {"never": "Sombra"}}})

    def test_an_empty_name_override_entry_is_rejected(self):
        with self.assertRaisesRegex(MonitorError, "always"):
            self.load({"goods": {"Oil": {"always": ["  "]}}})

    def test_a_non_object_goods_section_is_rejected(self):
        with self.assertRaisesRegex(MonitorError, "market.goods"):
            self.load({"goods": ["Oil"]})

    def test_a_whitespace_only_good_key_is_rejected(self):
        with self.assertRaisesRegex(MonitorError, "market.goods"):
            self.load({"goods": {"   ": {}}})

    def test_a_non_object_market_section_is_rejected(self):
        for market in [None, ["Oil"], "Oil"]:
            with self.subTest(market=market):
                with self.assertRaisesRegex(MonitorError, "market"):
                    self.load(market)

    def test_a_commented_out_never_name_is_dropped(self):
        settings = self.load({"goods": {"Oil": {"never": ["# Sombra", "Luna Sueno"]}}})
        self.assertEqual(settings.alerts.market_goods[0].never, ("Luna Sueno",))

    def test_name_overrides_that_are_entirely_commented_out_are_empty(self):
        settings = self.load(
            {"goods": {"Oil": {"always": ["# Luna Sueno"], "never": ["# Sombra"]}}}
        )
        self.assertEqual(settings.alerts.market_goods[0].always, ())
        self.assertEqual(settings.alerts.market_goods[0].never, ())

    def test_a_misspelled_knob_is_rejected_rather_than_silently_ignored(self):
        # Ignoring it would silently mean friends=True, the opposite of what was written,
        # with nothing in the file to show why the alerts keep coming.
        with self.assertRaisesRegex(MonitorError, "freinds"):
            self.load({"goods": {"Oil": {"freinds": False}}})

    def test_an_unknown_knob_is_rejected_by_naming_the_valid_ones(self):
        with self.assertRaisesRegex(
            MonitorError, "alliance, always, friends, never, reserve, reserve_amount"
        ):
            self.load({"goods": {"Oil": {"freinds": False}}})

    def test_two_good_keys_differing_only_by_case_are_rejected(self):
        with self.assertRaisesRegex(MonitorError, "'Oil'.*'oil'"):
            self.load({"goods": {"Oil": {}, "oil": {}}})

    def test_a_commented_out_good_does_not_collide_with_the_watched_one(self):
        settings = self.load({"goods": {"# Oil": {}, "Oil": {}}})
        self.assertEqual([good.name for good in settings.alerts.market_goods], ["Oil"])

    def test_an_absent_market_section_is_reported_as_a_default(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"sound": {"wav_path": None}}), encoding="utf-8")
            settings = load_settings(path)
        self.assertEqual(settings.alerts.market_goods, ())
        self.assertIn("market.goods", settings.defaults_used)

    def test_market_orders_category_can_be_disabled(self):
        value = {"sound": {"wav_path": None}, "alerts": {"market_orders": False}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            settings = load_settings(path)
        self.assertFalse(settings.alerts.market_orders)


#: Every tradeable good in the game (resourcedefs.is_tradeable = 1), in the order the
#: buyer's-marketplace selector lists them: alphabetical, with the DNA strains last.
TRADEABLE_GOODS = [
    "Apples",
    "Cider",
    "Coffee",
    "Composites",
    "Copper",
    "Drugs",
    "Gasoline",
    "Gems",
    "Machinery Parts",
    "Oil",
    "Pies",
    "Plastics",
    "Precision Parts",
    "Toys",
    "Tungsten",
    "Vehicle Parts",
    "DNA - Central Burrozil",
    "DNA - Central Przewalskia",
    "DNA - Central Saddle Arabia",
    "DNA - Central Zebrica",
    "DNA - North Burrozil",
    "DNA - North Przewalskia",
    "DNA - North Saddle Arabia",
    "DNA - North Zebrica",
    "DNA - South Burrozil",
    "DNA - South Przewalskia",
    "DNA - South Saddle Arabia",
    "DNA - South Zebrica",
]


class ShippedMarketGoodsTests(unittest.TestCase):
    def test_every_tradeable_good_ships_commented_out(self):
        goods = shipped_example()["market"]["goods"]
        self.assertEqual(list(goods), [f"# {name}" for name in TRADEABLE_GOODS])

    def test_every_shipped_good_shows_all_reserve_and_buyer_knobs(self):
        for key, good in shipped_example()["market"]["goods"].items():
            with self.subTest(good=key):
                self.assertEqual(
                    sorted(good),
                    ["alliance", "always", "friends", "never", "reserve", "reserve_amount"],
                )
                self.assertTrue(good["friends"])
                self.assertTrue(good["alliance"])
                self.assertEqual(good["always"], [])
                self.assertEqual(good["never"], [])
                self.assertEqual(good["reserve"], "none")
                self.assertEqual(good["reserve_amount"], 0)

    def test_shipped_as_is_the_example_watches_nothing(self):
        settings = load_settings(Path("settings.example.json"))
        self.assertEqual(settings.alerts.market_goods, ())
        self.assertTrue(settings.alerts.market_orders)

    def test_uncommenting_a_shipped_good_switches_it_on(self):
        value = shipped_example()
        value["market"]["goods"] = {
            (key[2:] if key == "# Machinery Parts" else key): good
            for key, good in value["market"]["goods"].items()
        }
        # The bundled WAV is reached by a path relative to the settings file, which a temp
        # directory has no copy of; the sound is irrelevant here.
        value["sound"]["wav_path"] = None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            settings = load_settings(path)
        self.assertEqual(
            [good.name for good in settings.alerts.market_goods], ["Machinery Parts"]
        )


def market_row(nation_id, name, colour, amount, price, region="Saddle Arabia"):
    """One deals-table row shaped exactly as buyermarketplace.php renders it."""
    if colour:
        shown_name = f'<span class="{colour}">{name}</span>'
        shown_region = f'<span class="{colour}">{region}</span>'
    else:
        shown_name, shown_region = name, region
    return f"""
<tr><td><div class="row">
  <div class="col-md-1"><p class="text-danger">{price}</p></div>
  <div class="col-md-1"><p class="text-success">{amount}</p></div>
  <div class="col-md-5"><p><a href="viewnation.php?nation_id={nation_id}">{shown_name}
    (<img src="images/icons/Oil.png"/>{shown_region})</a></p></div>
  <div class="col-md-5"><div class="row"><div class="col-xs-6">
    <form action="buyermarketplace.php" method="post">
    <input type="hidden" name="resource_id" value="10"/>
    <input type="submit" name="sellone" value="Sell One" class="btn btn-primary"/>
    </form></div></div></div>
</div></td></tr>
"""


MARKET_TABLE_HEAD = """
<table class="table table-striped table-bordered">
<thead><tr><td><div class="row">
  <div class="col-md-1">Offering</div>
  <div class="col-md-1">Amount</div>
  <div class="col-md-5">Buyer</div>
  <div class="col-md-5">Actions</div>
</div></td></tr></thead><tbody>
"""


def market_page(*rows):
    return MARKET_TABLE_HEAD + "".join(rows) + "</tbody></table>"


class MarketRowParsingTests(unittest.TestCase):
    def parse(self, html, roster=None):
        return clop_monitor.parse_market_orders(html, "Machinery Parts", roster)

    def test_a_friend_row_is_read_as_a_friend(self):
        orders = self.parse(market_page(market_row(42, "Luna Sueno", "text-info", 12, "5,000")))
        self.assertEqual(len(orders), 1)
        order = orders[0]
        self.assertEqual(order.good, "Machinery Parts")
        self.assertEqual(order.nation_id, 42)
        self.assertEqual(order.nation_name, "Luna Sueno")
        self.assertEqual(order.amount, 12)
        self.assertEqual(order.price, 5000)
        self.assertTrue(order.is_friend)
        self.assertFalse(order.is_enemy)

    def test_an_alliance_row_is_read_as_an_ally(self):
        orders = self.parse(market_page(market_row(7, "Ally Nation", "text-success", 3, "4,800")))
        self.assertTrue(orders[0].is_ally)
        self.assertFalse(orders[0].is_friend)

    def test_an_enemy_row_is_read_as_an_enemy(self):
        orders = self.parse(market_page(market_row(9, "Sombra", "text-danger", 1, "100")))
        self.assertTrue(orders[0].is_enemy)
        self.assertFalse(orders[0].is_friend)

    def test_an_unstyled_row_has_no_relation_despite_the_coloured_price_and_amount_cells(self):
        # The colour trap: the price cell is text-danger and the amount cell text-success in
        # every row, so a page-wide class match would call this stranger an enemy and an ally.
        orders = self.parse(market_page(market_row(5, "Some Stranger", "", 2, "9,000")))
        self.assertEqual(orders[0].nation_name, "Some Stranger")
        self.assertFalse(orders[0].is_friend)
        self.assertFalse(orders[0].is_enemy)
        self.assertFalse(orders[0].is_ally)

    def test_the_header_row_is_not_an_order(self):
        self.assertEqual(self.parse(market_page()), [])

    def test_every_row_is_returned_in_page_order(self):
        orders = self.parse(
            market_page(
                market_row(1, "First", "text-info", 1, "9,000"),
                market_row(2, "Second", "text-success", 2, "8,000"),
                market_row(3, "Third", "", 3, "7,000"),
            )
        )
        self.assertEqual([order.nation_name for order in orders], ["First", "Second", "Third"])

    def test_a_roster_decides_alliance_regardless_of_colour(self):
        html = market_page(
            market_row(42, "Friend And Ally", "text-info", 1, "1,000"),
            market_row(43, "Friend Only", "text-info", 1, "1,000"),
            market_row(44, "Enemy And Ally", "text-danger", 1, "1,000"),
        )
        orders = self.parse(html, roster=frozenset({42, 44}))
        self.assertEqual([order.is_ally for order in orders], [True, False, True])
        self.assertEqual([order.is_friend for order in orders], [True, True, False])

    def test_without_a_roster_only_green_reads_as_an_ally(self):
        html = market_page(
            market_row(42, "Friend", "text-info", 1, "1,000"),
            market_row(7, "Ally", "text-success", 1, "1,000"),
        )
        self.assertEqual([order.is_ally for order in self.parse(html)], [False, True])


class UnreadableMarketRowTests(unittest.TestCase):
    """A row the parser cannot read is dropped rather than raised on.

    Killing the whole poll over one odd row would lose the alerts from every other row and
    every other category; a page that is not the marketplace at all is caught earlier, by the
    missing CSRF token.
    """

    def parse(self, html):
        return clop_monitor.parse_market_orders(html, "Machinery Parts", None)

    def test_a_row_missing_its_amount_cell_is_dropped(self):
        row = market_row(42, "Luna Sueno", "text-info", 12, "5,000")
        without_amount = row.replace(
            '<div class="col-md-1"><p class="text-success">12</p></div>', ""
        )
        self.assertNotEqual(without_amount, row)
        self.assertEqual(self.parse(market_page(without_amount)), [])

    def test_a_row_whose_price_is_not_a_number_is_dropped(self):
        self.assertEqual(
            self.parse(market_page(market_row(42, "Luna Sueno", "text-info", 12, "Sold Out"))),
            [],
        )

    def test_a_truncated_page_yields_only_its_complete_rows(self):
        cut_off = market_row(2, "Second", "text-info", 2, "8,000")
        cut_off = cut_off[: cut_off.index("</tr>")]
        html = MARKET_TABLE_HEAD + market_row(1, "First", "text-info", 1, "9,000") + cut_off
        self.assertEqual([order.nation_name for order in self.parse(html)], ["First"])

    def test_an_error_page_with_no_table_yields_no_orders(self):
        self.assertEqual(self.parse("<h1>Something went wrong</h1>"), [])


class MarketNumberTests(unittest.TestCase):
    def test_thousands_separators_are_stripped_from_a_price(self):
        self.assertEqual(clop_monitor.parse_market_number("5,000"), 5000)

    def test_plain_digits_are_read_as_they_are(self):
        self.assertEqual(clop_monitor.parse_market_number("12"), 12)

    def test_an_empty_cell_is_unreadable(self):
        self.assertIsNone(clop_monitor.parse_market_number(""))

    def test_a_non_numeric_cell_is_unreadable(self):
        self.assertIsNone(clop_monitor.parse_market_number("Sold Out"))

    def test_a_negative_is_unreadable(self):
        # The game renders neither a negative price nor a negative amount, so a minus sign
        # means the cell is not the one we think it is; reading it as a number would be worse.
        self.assertIsNone(clop_monitor.parse_market_number("-5"))


class RelationLabelTests(unittest.TestCase):
    def label(self, **flags):
        return clop_monitor.MarketOrder("Oil", 1, "N", 1, 1, **flags).relation_label()

    def test_a_friend_who_is_also_an_ally_is_labelled_as_both(self):
        self.assertEqual(self.label(is_friend=True, is_ally=True), "friend, alliance")

    def test_a_buyer_with_no_relation_says_so(self):
        self.assertEqual(self.label(), "no relation")

    def test_an_enemy_is_named(self):
        self.assertEqual(self.label(is_enemy=True), "enemy")


MARKET_FORM = """
<form action="buyermarketplace.php" method="post" class="form-inline">
  <input type="hidden" name="token_buyermarketplace" value="abc123"/>
  <input type="hidden" name="mode" value=""/>
  <select name="resource_id" class="form-control">
    <option value=""></option>
    <option value="3">Apples</option>
    <option value="10" selected >Machinery Parts (Have 5)</option>
    <option value="1">Oil</option>
  </select>
</form>
"""

MULTI_NATION_HEADER = """
<li><a><form action="" method="post"><select name="switchnation_id">
<option value="11">First Nation</option>
<option value="12" selected >Second Nation</option>
</select></form></a></li>
"""

EMPIRE_OVERVIEW = """
<table><tr>
<td><div title="First Nation"><form action="overview.php" method="post">
<button name="switchnation_id" type="submit" value="12">First Nation</button></form></div></td>
<td><div title="Second Nation"><form action="overview.php" method="post">
<button name="switchnation_id" type="submit" value="13">Second Nation</button></form></div></td>
<td><div title="Third Nation"><form action="overview.php" method="post">
<button name="switchnation_id" type="submit" value="14">Third Nation</button></form></div></td>
</tr></table>
"""

#: The single-nation account, which is the case header.php gives no nation switcher, so the
#: empire overview is the only place its id can be read.
SINGLE_NATION_EMPIRE_OVERVIEW = """
<table><tr>
<td><div title="Only Nation"><form action="overview.php" method="post">
<button name="switchnation_id" type="submit" value="12">Only Nation</button></form></div></td>
</tr></table>
"""

NATION_PAGE = """
<center><h4>Alliance:
<a href="viewalliance.php?alliance_id=7">The Best Alliance</a></h4></center>
"""

ALLIANCE_PAGE = """
<table>
<tr><td><a href="viewuser.php?user_id=1">somepony</a></td>
<td><a href="viewnation.php?nation_id=12">Mine (<img src="x.png"/>Zebrica)</a>
<a href="viewnation.php?nation_id=13">Also Mine (<img src="x.png"/>Zebrica)</a></td></tr>
<tr><td><a href="viewuser.php?user_id=2">otherpony</a></td>
<td><a href="viewnation.php?nation_id=42">Theirs (<img src="x.png"/>Burrozil)</a></td></tr>
</table>
"""


class MarketFormParsingTests(unittest.TestCase):
    def test_the_csrf_token_is_read_from_the_hidden_field(self):
        self.assertEqual(
            clop_monitor.parse_hidden_field(MARKET_FORM, "token_buyermarketplace"), "abc123"
        )

    def test_an_absent_hidden_field_is_none(self):
        self.assertIsNone(clop_monitor.parse_hidden_field(MARKET_FORM, "token_absent"))

    def test_a_visible_input_of_the_same_name_is_not_a_hidden_field(self):
        # Submit buttons share the form's names, and their value is the button label rather
        # than the state we came for, so reading one would silently POST the wrong thing.
        self.assertIsNone(
            clop_monitor.parse_hidden_field(
                '<input type="submit" name="mode" value="Go"/>', "mode"
            )
        )

    def test_a_visible_input_does_not_shadow_the_hidden_one_behind_it(self):
        html = '<input type="submit" name="mode" value="Go"/>' + MARKET_FORM
        self.assertEqual(clop_monitor.parse_hidden_field(html, "mode"), "")

    def test_good_names_map_to_resource_ids_without_the_have_suffix(self):
        self.assertEqual(
            clop_monitor.parse_good_ids(MARKET_FORM),
            {"Apples": 3, "Machinery Parts": 10, "Oil": 1},
        )

    def test_the_current_nation_comes_from_the_selected_header_option(self):
        self.assertEqual(clop_monitor.parse_current_nation_id(MULTI_NATION_HEADER), 12)

    def test_a_header_without_a_switcher_has_no_nation_id(self):
        self.assertIsNone(clop_monitor.parse_current_nation_id(AUTHENTICATED_HEADER))

    def test_the_empire_overview_lists_every_nation_button(self):
        # The whole point of this parser is the multi-nation account, so the fixture has to
        # have more than one button or returning just the first would pass.
        self.assertEqual(clop_monitor.parse_empire_nation_ids(EMPIRE_OVERVIEW), [12, 13, 14])

    def test_both_selects_on_one_page_are_read_apart(self):
        # Every real page includes header.php, so the market page carries the nation switcher
        # and the goods selector at once and each parser must ignore the other's select.
        page = MULTI_NATION_HEADER + MARKET_FORM
        self.assertEqual(clop_monitor.parse_current_nation_id(page), 12)
        self.assertEqual(
            clop_monitor.parse_good_ids(page),
            {"Apples": 3, "Machinery Parts": 10, "Oil": 1},
        )

    def test_both_selects_are_read_apart_whichever_comes_first(self):
        page = MARKET_FORM + MULTI_NATION_HEADER
        self.assertEqual(clop_monitor.parse_current_nation_id(page), 12)
        self.assertEqual(
            clop_monitor.parse_good_ids(page),
            {"Apples": 3, "Machinery Parts": 10, "Oil": 1},
        )

    def test_a_switcher_with_nothing_selected_has_no_nation_id(self):
        self.assertIsNone(
            clop_monitor.parse_current_nation_id(MULTI_NATION_HEADER.replace(" selected ", " "))
        )

    def test_a_nation_page_yields_its_alliance_id_and_name(self):
        self.assertEqual(clop_monitor.parse_alliance_link(NATION_PAGE), (7, "The Best Alliance"))

    def test_a_nation_page_without_an_alliance_link_yields_none(self):
        self.assertIsNone(clop_monitor.parse_alliance_link("<h4>Alliance: none</h4>"))

    def test_a_nation_in_no_alliance_yields_zero_rather_than_none(self):
        # viewnation.php:23 links unconditionally, so "no alliance" arrives as alliance 0 and
        # a caller testing `is not None` would go and fetch alliance 0.
        link = clop_monitor.parse_alliance_link(
            '<h4>Alliance: <a href="viewalliance.php?alliance_id=0">None</a></h4>'
        )
        self.assertEqual(link[0], 0)

    def test_the_alliance_page_yields_every_member_nation(self):
        self.assertEqual(
            clop_monitor.parse_alliance_nation_ids(ALLIANCE_PAGE),
            frozenset({12, 13, 42}),
        )


class MarketDecisionTests(unittest.TestCase):
    """never beats always; always beats both relation checks; the two checks are independent."""

    def order(self, name="Luna Sueno", **flags):
        return clop_monitor.MarketOrder("Oil", 1, name, 5, 1000, **flags)

    def decide(self, order, **knobs):
        stock = knobs.pop("stock", Stockpiles({"Oil": 5}, {"Oil": 3}))
        return clop_monitor.market_order_alerts(
            order, clop_monitor.WatchedGood("Oil", **knobs), stock
        )

    def test_a_friend_alerts_when_friends_is_on(self):
        self.assertTrue(self.decide(self.order(is_friend=True), friends=True, alliance=False))

    def test_a_friend_is_silent_when_friends_is_off(self):
        self.assertFalse(self.decide(self.order(is_friend=True), friends=False, alliance=False))

    def test_an_ally_alerts_when_alliance_is_on(self):
        self.assertTrue(self.decide(self.order(is_ally=True), friends=False, alliance=True))

    def test_an_ally_is_silent_when_alliance_is_off(self):
        self.assertFalse(self.decide(self.order(is_ally=True), friends=True, alliance=False))

    def test_a_friend_who_is_also_an_ally_alerts_on_the_alliance_check_alone(self):
        # The case that motivated splitting the checks: the game paints this buyer blue, so
        # a colour-only reading would have hidden them from "only my alliance".
        order = self.order(is_friend=True, is_ally=True)
        self.assertTrue(self.decide(order, friends=False, alliance=True))

    def test_a_friend_who_is_also_an_ally_alerts_on_the_friend_check_alone(self):
        order = self.order(is_friend=True, is_ally=True)
        self.assertTrue(self.decide(order, friends=True, alliance=False))

    def test_a_stranger_is_silent(self):
        self.assertFalse(self.decide(self.order(), friends=True, alliance=True))

    def test_an_enemy_is_silent(self):
        self.assertFalse(self.decide(self.order(is_enemy=True), friends=True, alliance=True))

    def test_an_always_name_alerts_with_both_checks_off(self):
        self.assertTrue(
            self.decide(self.order(), friends=False, alliance=False, always=("Luna %",))
        )

    def test_an_always_name_alerts_even_for_an_enemy(self):
        self.assertTrue(
            self.decide(self.order(is_enemy=True), friends=False, alliance=False,
                        always=("Luna Sueno",))
        )

    def test_a_never_name_silences_a_friend(self):
        self.assertFalse(
            self.decide(self.order(is_friend=True), friends=True, never=("Luna %",))
        )

    def test_never_beats_always(self):
        self.assertFalse(
            self.decide(self.order(), always=("Luna Sueno",), never=("Luna Sueno",))
        )

    def test_name_patterns_ignore_case(self):
        self.assertFalse(self.decide(self.order(is_friend=True), never=("luna sueno",)))

    def test_own_nation_is_silent_even_when_always_matches(self):
        stock = Stockpiles({"Oil": 5}, {"Oil": 3}, nation_name="LUNA SUENO")
        self.assertFalse(
            self.decide(
                self.order(is_friend=True, is_ally=True),
                stock=stock,
                always=("Luna %",),
            )
        )

    def test_none_reserve_still_requires_a_positive_quantity(self):
        self.assertFalse(
            self.decide(self.order(is_friend=True), stock=Stockpiles({"Oil": 0}))
        )
        self.assertTrue(
            self.decide(self.order(is_friend=True), stock=Stockpiles({"Oil": 1}))
        )

    def test_qty_reserve_requires_strictly_more_than_the_threshold(self):
        good = {"reserve": "qty", "reserve_amount": 5}
        self.assertFalse(
            self.decide(self.order(is_friend=True), stock=Stockpiles({"Oil": 5}), **good)
        )
        self.assertTrue(
            self.decide(self.order(is_friend=True), stock=Stockpiles({"Oil": 6}), **good)
        )

    def test_ticks_reserve_requires_strictly_more_than_the_threshold(self):
        good = {"reserve": "ticks", "reserve_amount": 3}
        self.assertFalse(
            self.decide(
                self.order(is_friend=True),
                stock=Stockpiles({"Oil": 10}, {"Oil": 3}),
                **good,
            )
        )
        self.assertTrue(
            self.decide(
                self.order(is_friend=True),
                stock=Stockpiles({"Oil": 10}, {"Oil": 4}),
                **good,
            )
        )

    def test_ticks_words_keep_their_game_meaning(self):
        good = {"reserve": "ticks", "reserve_amount": 3}
        self.assertTrue(
            self.decide(
                self.order(is_friend=True),
                stock=Stockpiles({"Oil": 10}, {"Oil": "N/A"}),
                **good,
            )
        )
        self.assertFalse(
            self.decide(
                self.order(is_friend=True),
                stock=Stockpiles({"Oil": 10}, {"Oil": "NONE"}),
                **good,
            )
        )

    def test_ticks_reserve_never_alerts_with_zero_quantity_even_if_ticks_are_na(self):
        self.assertFalse(
            self.decide(
                self.order(is_friend=True),
                reserve="ticks",
                reserve_amount=0,
                stock=Stockpiles({}, {}),
            )
        )

    def test_ticks_reserve_reports_a_missing_ticks_column_as_a_monitor_error(self):
        with self.assertRaisesRegex(MonitorError, "no Ticks-Worth column"):
            self.decide(
                self.order(is_friend=True),
                reserve="ticks",
                reserve_amount=0,
                stock=Stockpiles({"Oil": 10}),
            )


class MarketAlertTests(unittest.TestCase):
    def snapshot(self, *orders):
        quantities = {order.good: max(order.amount, 1) for order in orders}
        return Snapshot(
            0,
            0,
            None,
            market_orders=tuple(orders),
            stockpiles=Stockpiles(quantities),
        )

    def settings(self, *goods):
        return AlertCategorySettings(market_goods=tuple(goods))

    def test_matching_orders_become_one_block_per_good(self):
        current = self.snapshot(
            clop_monitor.MarketOrder("Machinery Parts", 42, "Luna Sueno", 12, 5000,
                                     is_friend=True),
            clop_monitor.MarketOrder("Machinery Parts", 7, "Ally Nation", 3, 4800,
                                     is_ally=True),
        )
        alerts = build_alerts(None, current, self.settings(clop_monitor.WatchedGood("Machinery Parts")))
        self.assertEqual(
            alerts,
            [
                "Buy orders for Machinery Parts:\n"
                "  Luna Sueno (friend) wants 12 at 5,000 bits each\n"
                "  Ally Nation (alliance) wants 3 at 4,800 bits each\n"
                "https://4clop.org/buyermarketplace.php"
            ],
        )

    def test_a_buyer_who_is_both_is_labelled_as_both(self):
        current = self.snapshot(
            clop_monitor.MarketOrder("Oil", 42, "Both Nation", 40, 4500,
                                     is_friend=True, is_ally=True),
        )
        alerts = build_alerts(None, current, self.settings(clop_monitor.WatchedGood("Oil")))
        self.assertIn("Both Nation (friend, alliance) wants 40 at 4,500 bits each", alerts[0])

    def test_an_enemy_reached_through_always_is_labelled_as_an_enemy(self):
        current = self.snapshot(
            clop_monitor.MarketOrder("Oil", 9, "Sombra", 6, 300, is_enemy=True)
        )
        good = clop_monitor.WatchedGood("Oil", always=("Sombra",))
        alerts = build_alerts(None, current, self.settings(good))
        self.assertIn("Sombra (enemy) wants 6 at 300 bits each", alerts[0])

    def test_a_buyer_with_no_relation_reached_through_always_says_so(self):
        current = self.snapshot(clop_monitor.MarketOrder("Oil", 5, "Stranger", 2, 900))
        good = clop_monitor.WatchedGood("Oil", always=("Stranger",))
        alerts = build_alerts(None, current, self.settings(good))
        self.assertIn("Stranger (no relation) wants 2 at 900 bits each", alerts[0])

    def test_a_good_with_no_matching_orders_produces_no_block(self):
        current = self.snapshot(clop_monitor.MarketOrder("Oil", 5, "Stranger", 1, 100))
        self.assertEqual(
            build_alerts(None, current, self.settings(clop_monitor.WatchedGood("Oil"))), []
        )

    def test_a_good_with_no_orders_at_all_produces_no_block(self):
        self.assertEqual(
            build_alerts(
                None, self.snapshot(), self.settings(clop_monitor.WatchedGood("Oil"))
            ),
            [],
        )

    def test_an_unwatched_good_is_silently_ignored(self):
        # The poll POSTs for the watched goods and nothing else, so an order for anything
        # else can only be a bug; dropping it silently is deliberate rather than an oversight.
        current = self.snapshot(
            clop_monitor.MarketOrder("Gems", 1, "A", 1, 100, is_friend=True)
        )
        self.assertEqual(
            build_alerts(None, current, self.settings(clop_monitor.WatchedGood("Oil"))), []
        )

    def test_a_good_matches_the_games_spelling_whatever_case_the_settings_use(self):
        # market_preflight stamps orders with the game's spelling, not the one typed into
        # settings.json, so a lowercase setting must still find its own orders.
        current = self.snapshot(
            clop_monitor.MarketOrder("Machinery Parts", 1, "A", 1, 100, is_friend=True)
        )
        alerts = build_alerts(
            None, current, self.settings(clop_monitor.WatchedGood("machinery parts"))
        )
        self.assertEqual(len(alerts), 1)
        self.assertTrue(alerts[0].startswith("Buy orders for machinery parts:"))

    def test_each_good_gets_its_own_block_in_the_order_the_settings_list_them(self):
        # The orders arrive in the opposite order to the settings, so this fails if the
        # blocks follow the orders rather than the watch list.
        current = self.snapshot(
            clop_monitor.MarketOrder("Apples", 2, "B", 2, 200, is_friend=True),
            clop_monitor.MarketOrder("Oil", 1, "A", 1, 100, is_friend=True),
        )
        alerts = build_alerts(
            None,
            current,
            self.settings(clop_monitor.WatchedGood("Oil"), clop_monitor.WatchedGood("Apples")),
        )
        self.assertEqual(len(alerts), 2)
        self.assertTrue(alerts[0].startswith("Buy orders for Oil:"))
        self.assertTrue(alerts[1].startswith("Buy orders for Apples:"))

    def test_the_market_category_can_be_disabled(self):
        current = self.snapshot(
            clop_monitor.MarketOrder("Oil", 1, "A", 1, 100, is_friend=True)
        )
        settings = AlertCategorySettings(
            market_orders=False, market_goods=(clop_monitor.WatchedGood("Oil"),)
        )
        self.assertEqual(build_alerts(None, current, settings), [])

    def test_market_orders_alert_on_every_poll_while_pending(self):
        order = clop_monitor.MarketOrder("Oil", 1, "A", 1, 100, is_friend=True)
        settings = self.settings(clop_monitor.WatchedGood("Oil"))
        previous = self.snapshot(order)
        current = self.snapshot(order)
        self.assertEqual(len(build_alerts(previous, current, settings)), 1)

    def test_market_orders_are_not_persisted(self):
        snapshot = self.snapshot(
            clop_monitor.MarketOrder("Oil", 1, "A", 1, 100, is_friend=True)
        )
        self.assertNotIn("market_orders", snapshot.to_json())


#: buyermarketplace.php:162-166 renders this only under `$_POST['resource_id'] && empty($errors)`,
#: so it is the game's positive marker for "the request ran and nobody is buying".
EMPTY_MARKET_BANNER = '<div class="alert alert-warning">Nobody wants to buy that item.</div>'

#: What the page returns when it refuses the POST: the form comes back (with a rotated token,
#: because backend_buyermarketplace.php:55-57 rotates unconditionally) but the deals SELECT at
#: :264 sits inside `if (!$errors)`, so there is neither a table nor the empty-market banner.
MARKET_ERROR_PAGE = MARKET_FORM + '<div class="alert alert-danger">Try again.</div>'


def market_responder(rows_by_resource_id):
    """buyermarketplace.php as the game serves it, keyed on the good the POST asked for.

    The GET renders the form alone; the order table exists only for a POST, filtered to that
    POST's resource_id. Serving the same page to both would let an implementation that read
    the GET's HTML for every good pass.
    """

    def serve(form):
        if form is None:
            return MARKET_FORM
        rows = rows_by_resource_id.get(form["resource_id"], ())
        return MARKET_FORM + (market_page(*rows) if rows else EMPTY_MARKET_BANNER)

    return serve


def market_overview(amounts=None, ticks=None):
    """A valid overview carrying enough stock data for market-reserve decisions."""
    amounts = amounts or {"Machinery Parts": 100, "Oil": 100, "Apples": 100}
    ticks = ticks or {}
    rows = "".join(
        f'<tr><td style="text-align: right;">{name}</td>'
        f"<td>{amount}</td><td>0</td><td>0</td><td>0</td><td>0</td>"
        f"<td>{ticks.get(name, 'N/A')}</td></tr>"
        for name, amount in amounts.items()
    )
    return (
        AUTHENTICATED_HEADER
        + "<center><h3>Our Nation</h3></center>"
        + '<div class="panel-heading">Resources</div><table>'
        '<tr><td style="text-align: right;">Resource</td><td>Qty</td><td>Generated</td>'
        "<td>Used</td><td>Loss</td><td>Net</td><td>Ticks-Worth</td></tr>"
        + rows
        + "</table>"
        '<div class="panel-heading">Buildings</div><table>'
        '<tr><td style="text-align: right;">Farm</td><td><span>1</span></td></tr>'
        "</table></html>"
    )


def market_client(pages, goods=(("Machinery Parts", 10),), alliance_id=None):
    """A client whose _open serves canned pages and records every call.

    A page may be a string or a callable taking the POST form, which is None for a GET, so a
    test can serve a path's GET and POST differently the way the real page does.
    """
    client = ClopClient("https://4clop.org", "user", "password")
    client.market_goods = goods
    client.alliance_id = alliance_id
    calls = []

    def fake_open(path, form=None):
        calls.append((path, form))
        if path == "myalliance.php":
            raise AssertionError("myalliance.php marks alliance messages read")
        if path not in pages:
            raise AssertionError(f"Unexpected path: {path}")
        page = pages[path]
        return page(form) if callable(page) else page

    client._open = fake_open
    return client, calls


class MarketFetchTests(unittest.TestCase):
    def test_each_post_spends_the_token_from_the_previous_response(self):
        tokens = iter(["token-1", "token-2", "token-3"])

        def form(posted=None):
            page = MARKET_FORM.replace("abc123", next(tokens))
            return page if posted is None else page + EMPTY_MARKET_BANNER

        client, calls = market_client(
            {"buyermarketplace.php": form},
            goods=(("Apples", 3), ("Oil", 1)),
        )
        client._market_orders(None)
        posts = [form_data for path, form_data in calls if form_data is not None]
        self.assertEqual(
            [post["token_buyermarketplace"] for post in posts], ["token-1", "token-2"]
        )
        self.assertEqual([post["resource_id"] for post in posts], ["3", "1"])
        self.assertEqual([post["mode"] for post in posts], ["", ""])

    def test_a_market_post_carries_nothing_that_could_change_the_game(self):
        # offer, remove, sellone, sellall and sellamount are the only fields that make
        # backend_buyermarketplace.php spend funds, delete orders or sell goods; without
        # them the POST is a pure filter-and-display of the deals table.
        client, calls = market_client({"buyermarketplace.php": market_responder({})})
        client._market_orders(None)
        posts = [form_data for _, form_data in calls if form_data is not None]
        self.assertEqual(len(posts), 1)
        self.assertEqual(
            sorted(posts[0]), ["mode", "resource_id", "token_buyermarketplace"]
        )

    def test_orders_are_tagged_with_the_good_that_was_requested(self):
        client, _ = market_client(
            {
                "buyermarketplace.php": market_responder(
                    {"10": [market_row(42, "Luna Sueno", "text-info", 12, "5,000")]}
                )
            }
        )
        orders = client._market_orders(None)
        self.assertEqual([order.good for order in orders], ["Machinery Parts"])
        self.assertEqual(orders[0].nation_name, "Luna Sueno")

    def test_a_missing_token_is_a_monitor_error(self):
        client, _ = market_client({"buyermarketplace.php": "<form></form>"})
        with self.assertRaisesRegex(MonitorError, "CSRF token"):
            client._market_orders(None)

    def test_each_goods_orders_come_from_its_own_response(self):
        # The table is filtered server-side to the posted resource_id, so reading the wrong
        # response would attribute one good's buyers to another.
        client, _ = market_client(
            {
                "buyermarketplace.php": market_responder(
                    {
                        "3": [market_row(42, "Apple Buyer", "text-info", 12, "5,000")],
                        "1": [market_row(43, "Oil Buyer", "text-success", 3, "4,800")],
                    }
                )
            },
            goods=(("Apples", 3), ("Oil", 1)),
        )
        orders = client._market_orders(None)
        self.assertEqual(
            [(order.good, order.nation_name) for order in orders],
            [("Apples", "Apple Buyer"), ("Oil", "Oil Buyer")],
        )

    def test_the_request_shape_is_one_get_plus_one_post_per_good(self):
        # Pins the N+1 shape so a refactor cannot quietly make it N squared.
        goods = (("Apples", 3), ("Oil", 1), ("Pies", 5), ("Gems", 7), ("Copper", 9))
        client, calls = market_client(
            {"buyermarketplace.php": market_responder({})}, goods=goods
        )
        client._market_orders(None)
        market_calls = [form for path, form in calls if path == "buyermarketplace.php"]
        self.assertEqual(len(market_calls), len(goods) + 1)
        self.assertEqual([form is None for form in market_calls], [True] + [False] * len(goods))

    def test_a_genuinely_empty_market_is_no_orders_and_no_error(self):
        client, _ = market_client({"buyermarketplace.php": market_responder({})})
        self.assertEqual(client._market_orders(None), ())

    def test_a_rejected_post_on_a_non_final_good_is_not_read_as_no_orders(self):
        # The page rotates its token even when it rejects the POST, so the NEXT good's POST
        # succeeds and the loop self-heals: without a check here the rejected good silently
        # contributes zero orders and nothing ever reports it.
        def serve(form):
            if form is None:
                return MARKET_FORM
            if form["resource_id"] == "3":
                return MARKET_ERROR_PAGE
            return MARKET_FORM + market_page(market_row(1, "Oil Buyer", "text-info", 1, "100"))

        client, _ = market_client(
            {"buyermarketplace.php": serve}, goods=(("Apples", 3), ("Oil", 1))
        )
        with self.assertRaisesRegex(MonitorError, "Apples"):
            client._market_orders(None)

    def test_a_rejected_post_on_the_final_good_is_not_read_as_no_orders(self):
        def serve(form):
            if form is None:
                return MARKET_FORM
            if form["resource_id"] == "1":
                return MARKET_ERROR_PAGE
            return MARKET_FORM + market_page(market_row(1, "Apple Buyer", "text-info", 1, "100"))

        client, _ = market_client(
            {"buyermarketplace.php": serve}, goods=(("Apples", 3), ("Oil", 1))
        )
        with self.assertRaisesRegex(MonitorError, "Oil"):
            client._market_orders(None)

    def test_a_login_page_served_to_the_last_post_is_not_read_as_no_orders(self):
        # A session that dies mid-loop gets the login form back, which has neither the order
        # table nor the empty-market banner.
        login_page = '<form action="login.php" method="post"><input name="username"/></form>'

        def serve(form):
            if form is None:
                return MARKET_FORM
            if form["resource_id"] == "1":
                return login_page
            return MARKET_FORM + market_page(market_row(1, "Apple Buyer", "text-info", 1, "100"))

        client, _ = market_client(
            {"buyermarketplace.php": serve}, goods=(("Apples", 3), ("Oil", 1))
        )
        with self.assertRaisesRegex(MonitorError, "Oil"):
            client._market_orders(None)

    def test_an_unresolved_alliance_is_an_error_not_an_empty_roster(self):
        # None means "never resolved", which is a different fact from "in no alliance"; a
        # caller that got frozenset() for it would silently lose every ally.
        client, calls = market_client({}, alliance_id=None)
        with self.assertRaisesRegex(MonitorError, "not been resolved"):
            client._alliance_roster()
        self.assertEqual(calls, [])

    def test_the_roster_comes_from_viewalliance_never_myalliance(self):
        client, calls = market_client(
            {"viewalliance.php?alliance_id=7": ALLIANCE_PAGE}, alliance_id=7
        )
        self.assertEqual(client._alliance_roster(), frozenset({12, 13, 42}))
        self.assertEqual([path for path, _ in calls], ["viewalliance.php?alliance_id=7"])

    def test_no_alliance_yields_an_empty_roster_without_a_request(self):
        client, calls = market_client({}, alliance_id=0)
        self.assertEqual(client._alliance_roster(), frozenset())
        self.assertEqual(calls, [])

    def test_an_empty_fetched_roster_is_a_failure_not_a_membership_of_none(self):
        # Your own nation is always on your own alliance page, so nothing there means the
        # fetch failed. Returning it would demote every ally to a stranger and silently
        # stop the alerts this feature exists for.
        client, _ = market_client(
            {"viewalliance.php?alliance_id=7": "<h3>Alliance</h3><table></table>"},
            alliance_id=7,
        )
        with self.assertRaisesRegex(MonitorError, "listed no member nations"):
            client._alliance_roster()

    def test_a_snapshot_without_market_goods_makes_no_market_requests(self):
        navigation = AUTHENTICATED_HEADER.replace("(7)", "").replace("(2)", "")
        client, calls = market_client(
            {
                "index.php": navigation,
                "news.php?page=1": navigation + "<h3>News</h3>No news yet.",
                "reports.php": navigation + "<h3>Reports</h3><table></table>",
            },
            goods=(),
        )
        self.assertEqual(client.snapshot().market_orders, ())
        self.assertNotIn("buyermarketplace.php", [path for path, _ in calls])

    def test_include_market_false_skips_the_market_requests(self):
        navigation = AUTHENTICATED_HEADER.replace("(7)", "").replace("(2)", "")
        client, calls = market_client(
            {
                "index.php": navigation,
                "news.php?page=1": navigation + "<h3>News</h3>No news yet.",
                "reports.php": navigation + "<h3>Reports</h3><table></table>",
            }
        )
        client.snapshot(include_market=False)
        self.assertNotIn("buyermarketplace.php", [path for path, _ in calls])

    def test_a_snapshot_with_goods_fetches_the_roster_and_the_orders(self):
        navigation = AUTHENTICATED_HEADER.replace("(7)", "").replace("(2)", "")
        client, calls = market_client(
            {
                "index.php": navigation,
                "news.php?page=1": navigation + "<h3>News</h3>No news yet.",
                "reports.php": navigation + "<h3>Reports</h3><table></table>",
                "buyermarketplace.php": market_responder(
                    {"10": [market_row(42, "Theirs", "text-danger", 12, "5,000")]}
                ),
                "viewalliance.php?alliance_id=7": ALLIANCE_PAGE,
                "overview.php": market_overview(),
            },
            alliance_id=7,
        )
        snapshot = client.snapshot()
        paths = [path for path, _ in calls]
        self.assertLess(paths.index("overview.php"), paths.index("buyermarketplace.php"))
        # Nation 42 is in the roster, so the red enemy colour does not hide their membership.
        self.assertEqual(len(snapshot.market_orders), 1)
        self.assertTrue(snapshot.market_orders[0].is_ally)
        self.assertTrue(snapshot.market_orders[0].is_enemy)

    def test_a_preparsed_stockpile_is_reused_without_an_overview_fetch(self):
        navigation = AUTHENTICATED_HEADER.replace("(7)", "").replace("(2)", "")
        client, calls = market_client(
            {
                "index.php": navigation,
                "news.php?page=1": navigation + "<h3>News</h3>No news yet.",
                "reports.php": navigation + "<h3>Reports</h3><table></table>",
                "buyermarketplace.php": market_responder({}),
            },
            alliance_id=0,
        )
        stock = Stockpiles({"Machinery Parts": 12})
        snapshot = client.snapshot(stockpiles=stock)
        self.assertIs(snapshot.stockpiles, stock)
        self.assertNotIn("overview.php", [path for path, _ in calls])


class MarketPreflightTests(unittest.TestCase):
    def client(self, pages):
        # Nothing is watched until the preflight resolves it, which is what these tests drive.
        return market_client(pages, goods=())

    def test_nothing_watched_makes_no_requests(self):
        client, calls = self.client({})
        self.assertIsNone(client.market_preflight(()))
        self.assertEqual(calls, [])

    def test_good_names_resolve_to_resource_ids(self):
        client, _ = self.client({"buyermarketplace.php": MARKET_FORM})
        message = client.market_preflight(
            (clop_monitor.WatchedGood("Machinery Parts", alliance=False),)
        )
        self.assertEqual(client.market_goods, (("Machinery Parts", 10),))
        self.assertIn("Machinery Parts", message)

    def test_good_names_match_regardless_of_case(self):
        client, _ = self.client({"buyermarketplace.php": MARKET_FORM})
        client.market_preflight((clop_monitor.WatchedGood("machinery PARTS", alliance=False),))
        self.assertEqual(client.market_goods, (("Machinery Parts", 10),))

    def test_an_unknown_good_is_fatal_and_names_itself(self):
        client, _ = self.client({"buyermarketplace.php": MARKET_FORM})
        with self.assertRaisesRegex(MonitorError, "Machinry Parts"):
            client.market_preflight((clop_monitor.WatchedGood("Machinry Parts", alliance=False),))

    def test_the_alliance_is_resolved_through_the_header_switcher(self):
        client, calls = self.client(
            {
                "buyermarketplace.php": MARKET_FORM,
                "index.php": MULTI_NATION_HEADER,
                "viewnation.php?nation_id=12": NATION_PAGE,
            }
        )
        message = client.market_preflight((clop_monitor.WatchedGood("Oil"),))
        self.assertEqual(client.alliance_id, 7)
        self.assertIn("The Best Alliance", message)
        self.assertNotIn("empireoverview.php", [path for path, _ in calls])

    def test_a_single_nation_account_falls_back_to_the_empire_overview(self):
        client, calls = self.client(
            {
                "buyermarketplace.php": MARKET_FORM,
                "index.php": AUTHENTICATED_HEADER,
                "empireoverview.php": SINGLE_NATION_EMPIRE_OVERVIEW,
                "viewnation.php?nation_id=12": NATION_PAGE,
            }
        )
        client.market_preflight((clop_monitor.WatchedGood("Oil"),))
        self.assertEqual(client.alliance_id, 7)
        self.assertIn("empireoverview.php", [path for path, _ in calls])

    def test_an_unidentifiable_nation_is_a_monitor_error(self):
        client, _ = self.client(
            {
                "buyermarketplace.php": MARKET_FORM,
                "index.php": AUTHENTICATED_HEADER,
                "empireoverview.php": SINGLE_NATION_EMPIRE_OVERVIEW.replace(
                    'value="12"', 'value=""'
                ),
            }
        )
        with self.assertRaisesRegex(MonitorError, "which nation is active"):
            client.market_preflight((clop_monitor.WatchedGood("Oil"),))

    def test_a_multi_nation_account_without_a_switcher_is_a_monitor_error(self):
        # Guessing the first of several nations would silently watch the wrong nation's
        # alliance, so an ambiguous account stops the monitor instead.
        client, _ = self.client(
            {
                "buyermarketplace.php": MARKET_FORM,
                "index.php": AUTHENTICATED_HEADER,
                "empireoverview.php": EMPIRE_OVERVIEW,
            }
        )
        with self.assertRaisesRegex(MonitorError, "which nation is active"):
            client.market_preflight((clop_monitor.WatchedGood("Oil"),))

    def test_no_alliance_is_reported_not_an_error(self):
        client, _ = self.client(
            {
                "buyermarketplace.php": MARKET_FORM,
                "index.php": MULTI_NATION_HEADER,
                "viewnation.php?nation_id=12":
                    '<h4>Alliance: <a href="viewalliance.php?alliance_id=0">None</a></h4>',
            }
        )
        message = client.market_preflight((clop_monitor.WatchedGood("Oil"),))
        self.assertEqual(client.alliance_id, 0)
        self.assertIn("no alliance", message)

    def test_an_unreadable_alliance_says_how_to_run_without_it(self):
        client, _ = self.client(
            {
                "buyermarketplace.php": MARKET_FORM,
                "index.php": MULTI_NATION_HEADER,
                "viewnation.php?nation_id=12": "<h4>Alliance: none</h4>",
            }
        )
        # A non-technical successor needs the way out, not just the diagnosis.
        with self.assertRaisesRegex(MonitorError, 'alliance": false'):
            client.market_preflight((clop_monitor.WatchedGood("Oil"),))

    def test_a_failed_preflight_leaves_nothing_half_applied(self):
        # Assigning the goods before the alliance is known would let a caller that logged the
        # error and carried on poll the market with alliance detection quietly degraded to
        # the green-colour heuristic the roster exists to replace.
        client, _ = self.client(
            {
                "buyermarketplace.php": MARKET_FORM,
                "index.php": MULTI_NATION_HEADER,
                "viewnation.php?nation_id=12": "<h4>Alliance: none</h4>",
            }
        )
        with self.assertRaises(MonitorError):
            client.market_preflight((clop_monitor.WatchedGood("Oil"),))
        self.assertEqual(client.market_goods, ())
        self.assertIsNone(client.alliance_id)

    def test_the_alliance_is_not_resolved_when_no_good_checks_it(self):
        client, calls = self.client({"buyermarketplace.php": MARKET_FORM})
        client.market_preflight((clop_monitor.WatchedGood("Oil", alliance=False),))
        self.assertIsNone(client.alliance_id)
        self.assertEqual([path for path, _ in calls], ["buyermarketplace.php"])


class MarketDuringAnAlertTests(unittest.TestCase):
    def test_the_refresh_after_a_dismissal_skips_the_market(self):
        """Dismissing a dialog re-reads the counts; replaying N market POSTs to do that
        would cost a request per watched good for information the refresh does not use."""
        calls = []

        class FakeClient:
            def snapshot(self, include_market=True):
                calls.append(include_market)
                return Snapshot(1, 0, None, None, None, False, (), ())

        class FakeNotifier:
            def notify(self, message):
                del message
                return True

        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            check_and_notify(FakeClient(), None, FakeNotifier(), state, persist_state=False)
        self.assertEqual(calls, [True, False])


class MutedMarketTests(unittest.TestCase):
    """Muting the category stops the market work, it does not just discard the result."""

    def preflight_goods(self, market_orders):
        """The goods main() hands the preflight for a given alerts.market_orders value."""
        return clop_monitor.goods_to_watch(
            AlertCategorySettings(
                market_orders=market_orders,
                market_goods=(clop_monitor.WatchedGood("Machinery Parts"),),
            )
        )

    def test_muting_the_category_watches_nothing(self):
        self.assertEqual(self.preflight_goods(False), ())

    def test_leaving_the_category_on_watches_the_configured_goods(self):
        self.assertEqual(
            [good.name for good in self.preflight_goods(True)], ["Machinery Parts"]
        )

    def test_a_muted_market_issues_no_requests_and_forgives_a_bad_good_name(self):
        # A watch list left in place while the category is off must not cost a request, and
        # must not turn a good-name typo into a fatal startup error for a switched-off
        # feature.
        client = ClopClient("https://4clop.org", "user", "password")

        def fake_open(path, form=None):
            raise AssertionError(f"A muted market must not request {path!r}")

        client._open = fake_open
        self.assertIsNone(client.market_preflight(self.preflight_goods(False)))
        self.assertEqual(client.market_goods, ())
        self.assertIsNone(client.alliance_id)


class OverrideNameIsTheNationTests(unittest.TestCase):
    """always and never match the Buyer column, which is the nation, never the username.

    The two are unrelated strings in CLOP: on the live game the player 'Lacera Viscera' fields
    the nation 'Fish Bucket'. A username written into either list matches nothing, and does so
    silently, so this pins which one the parser puts in front of the patterns.
    """

    def orders(self):
        return clop_monitor.parse_market_orders(
            market_page(market_row(26, "Fish Bucket", "text-success", 35, "1,000")),
            "Machinery Parts",
            frozenset({26}),
        )

    def test_the_buyer_column_supplies_the_nation_name(self):
        self.assertEqual(self.orders()[0].nation_name, "Fish Bucket")

    def test_never_silences_by_nation_name(self):
        good = clop_monitor.WatchedGood("Machinery Parts", never=("Fish Bucket",))
        self.assertFalse(
            clop_monitor.market_order_alerts(
                self.orders()[0], good, Stockpiles({"Machinery Parts": 1})
            )
        )

    def test_the_players_username_matches_nothing(self):
        good = clop_monitor.WatchedGood("Machinery Parts", never=("Lacera Viscera",))
        # The ally still alerts: the username never reached the comparison.
        self.assertTrue(
            clop_monitor.market_order_alerts(
                self.orders()[0], good, Stockpiles({"Machinery Parts": 1})
            )
        )


class MarketThroughAPollTests(unittest.TestCase):
    """The market halves joined: settings and pages in, a notifier message out.

    Every other check_and_notify test passes the default AlertCategorySettings, which watches
    no goods, so without these the market branch of build_alerts never runs inside the poll
    loop at all and the fetching, the alerting and the muting are each proven only alone.
    """

    #: No pending counts, no news and no reports, so a market block is the only alert a poll
    #: here can raise and the notifier needs no filtering.
    QUIET_NAVIGATION = AUTHENTICATED_HEADER.replace("(7)", "").replace("(2)", "")

    class RecordingNotifier:
        """Records what it was told and never blocks, so a poll runs straight through."""

        def __init__(self):
            self.messages = []

        def notify(self, message):
            self.messages.append(message)
            return False

    def pages(self, rows_by_resource_id):
        return {
            "index.php": self.QUIET_NAVIGATION,
            "news.php?page=1": self.QUIET_NAVIGATION + "<h3>News</h3>No news yet.",
            "reports.php": self.QUIET_NAVIGATION + "<h3>Reports</h3><table></table>",
            "buyermarketplace.php": market_responder(rows_by_resource_id),
            "overview.php": market_overview(),
        }

    def poll(self, client, previous, notifier, settings):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            current, _ = check_and_notify(
                client, previous, notifier, state, settings, persist_state=False
            )
        return current

    def test_a_pending_order_alerts_on_every_poll_it_is_still_pending_for(self):
        # The premise of the whole feature: an order is a standing fact, not an event, so an
        # unchanged second poll must alert again rather than treat it as already seen.
        client, _ = market_client(
            self.pages({"10": [market_row(42, "Luna Sueno", "text-info", 12, "5,000")]})
        )
        notifier = self.RecordingNotifier()
        settings = AlertCategorySettings(
            market_goods=(clop_monitor.WatchedGood("Machinery Parts"),)
        )
        first = self.poll(client, None, notifier, settings)
        second = self.poll(client, first, notifier, settings)
        self.assertEqual(first.market_orders, second.market_orders)
        self.assertEqual(len(notifier.messages), 2)
        for message in notifier.messages:
            self.assertIn("Buy orders for Machinery Parts:", message)
            self.assertIn("Luna Sueno (friend) wants 12 at 5,000 bits each", message)

    def test_the_overview_nation_never_appears_in_a_market_alert(self):
        client, _ = market_client(
            self.pages(
                {
                    "10": [
                        market_row(41, "Our Nation", "text-info", 99, "9,000"),
                        market_row(42, "Other Nation", "text-info", 12, "5,000"),
                    ]
                }
            )
        )
        notifier = self.RecordingNotifier()
        settings = AlertCategorySettings(
            market_goods=(
                clop_monitor.WatchedGood("Machinery Parts", always=("Our Nation",)),
            )
        )

        self.poll(client, None, notifier, settings)

        self.assertEqual(len(notifier.messages), 1)
        self.assertNotIn("Our Nation", notifier.messages[0])
        self.assertIn("Other Nation (friend) wants 12 at 5,000 bits each", notifier.messages[0])

    def test_a_muted_market_costs_no_request_across_a_whole_poll(self):
        # settings.json in, requests out: the marketplace is served here, so the only reason
        # for no call to it is that muting stopped the work rather than discarded its result.
        value = shipped_example()
        value["alerts"]["market_orders"] = False
        value["market"]["goods"] = {
            "Machinery Parts": {"friends": True, "alliance": True, "always": [], "never": []}
        }
        # The bundled WAV is reached relative to the settings file, which a temp directory
        # has no copy of; the sound is irrelevant here.
        value["sound"]["wav_path"] = None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            settings = load_settings(path)
        self.assertEqual(
            [good.name for good in settings.alerts.market_goods], ["Machinery Parts"]
        )

        client, calls = market_client(
            self.pages({"10": [market_row(42, "Luna Sueno", "text-info", 12, "5,000")]}),
            # Nothing is watched until the preflight resolves it, exactly as at startup.
            goods=(),
        )
        self.assertIsNone(
            client.market_preflight(clop_monitor.goods_to_watch(settings.alerts))
        )
        notifier = self.RecordingNotifier()
        current = self.poll(client, None, notifier, settings.alerts)
        self.assertEqual(current.market_orders, ())
        self.assertEqual(notifier.messages, [])
        self.assertEqual([path for path, _ in calls if path == "buyermarketplace.php"], [])

    def test_a_good_typed_in_the_wrong_case_survives_preflight_and_alerts(self):
        # The seam an earlier bug lived on: the preflight stores the game's spelling and the
        # orders are stamped with it, while the alert block is headed with the settings file's
        # spelling, so the two must be paired case-insensitively across the whole poll.
        client, _ = market_client(
            self.pages({"10": [market_row(42, "Luna Sueno", "text-info", 12, "5,000")]}),
            goods=(),
        )
        # alliance=False keeps the preflight off the nation and alliance pages, which this
        # test's fixtures do not serve and which the case question does not touch.
        good = clop_monitor.WatchedGood("machinery PARTS", alliance=False)
        message = client.market_preflight((good,))
        self.assertIn("watching Machinery Parts", message)
        self.assertEqual(client.market_goods, (("Machinery Parts", 10),))

        notifier = self.RecordingNotifier()
        current = self.poll(
            client, None, notifier, AlertCategorySettings(market_goods=(good,))
        )
        self.assertEqual([order.good for order in current.market_orders], ["Machinery Parts"])
        self.assertEqual(len(notifier.messages), 1)
        self.assertIn("Buy orders for machinery PARTS:", notifier.messages[0])
        self.assertIn("Luna Sueno (friend) wants 12 at 5,000 bits each", notifier.messages[0])


class ReloadNotifier:
    """A notifier that records what it was told instead of blocking on a dialog."""

    def __init__(self, sound=None):
        self.sound = sound
        self.failures = []

    def notify_failure(self, message):
        self.failures.append(message)
        return True


class SettingsChangeTests(unittest.TestCase):
    """What counts as a change, and what the confirmation line calls it."""

    def changes(self, **fields):
        return clop_monitor.settings_changes(clop_monitor.MonitorSettings(), fields.pop("to"))

    def test_an_untouched_file_changes_nothing(self):
        settings = clop_monitor.MonitorSettings()
        self.assertEqual(clop_monitor.settings_changes(settings, settings), ())

    def test_what_the_file_left_out_is_not_a_change(self):
        # defaults_used and file_found describe the file, not what the monitor is doing, so
        # they must not make an otherwise identical reload look like an edit.
        settings = clop_monitor.MonitorSettings()
        noisy = clop_monitor.replace(
            settings, defaults_used=("alerts.news",), file_found=False
        )
        self.assertEqual(clop_monitor.settings_changes(settings, noisy), ())

    def test_each_section_is_named_the_way_the_file_names_it(self):
        self.assertEqual(
            self.changes(to=clop_monitor.MonitorSettings(alerts=AlertCategorySettings(news=False))),
            ("alerts",),
        )
        self.assertEqual(
            self.changes(
                to=clop_monitor.MonitorSettings(
                    alerts=AlertCategorySettings(report_ignore=("Build % completed.",))
                )
            ),
            ("reports.ignore",),
        )
        self.assertEqual(
            self.changes(
                to=clop_monitor.MonitorSettings(
                    alerts=AlertCategorySettings(
                        market_goods=(clop_monitor.WatchedGood("Oil"),)
                    )
                )
            ),
            ("market.goods",),
        )
        self.assertEqual(
            self.changes(
                to=clop_monitor.MonitorSettings(
                    sound=clop_monitor.SoundSettings(loop_while_popup_open=True)
                )
            ),
            ("sound",),
        )
        self.assertEqual(
            self.changes(to=clop_monitor.MonitorSettings(cache=clop_monitor.CacheSettings(False))),
            ("cache",),
        )
        self.assertEqual(
            self.changes(
                to=clop_monitor.MonitorSettings(
                    fourchan_thread=parse_fourchan_thread_url(
                        "https://boards.4chan.org/mlp/thread/1"
                    )
                )
            ),
            ("fourchan.thread_url",),
        )


class SettingsChangeCompletenessTests(unittest.TestCase):
    """Every field of MonitorSettings is either compared or knowingly not compared.

    A field added later that nobody adds to settings_changes would mean edits to it never
    take effect while the monitor runs, silently and for good. Enumerating the dataclass
    rather than the comparison makes that an obligation the suite enforces instead of one a
    reviewer has to remember.
    """

    #: A value differing from the default, per field settings_changes compares.
    COMPARED = {
        "alerts": AlertCategorySettings(news=False),
        "sound": clop_monitor.SoundSettings(loop_while_popup_open=True),
        "cache": clop_monitor.CacheSettings(persist_to_file=False),
        "fourchan_thread": parse_fourchan_thread_url(
            "https://boards.4chan.org/mlp/thread/1"
        ),
    }

    #: Fields deliberately left out of the comparison, and why.
    NOT_COMPARED = {
        "defaults_used": "describes what the file omitted, not what the monitor is doing",
        "file_found": "handled as a true-to-false transition, not as a changed section",
    }

    def test_every_field_is_either_compared_or_listed_as_not_compared(self):
        self.assertEqual(
            {field.name for field in dataclasses.fields(clop_monitor.MonitorSettings)},
            set(self.COMPARED) | set(self.NOT_COMPARED),
        )

    def test_every_compared_field_actually_registers_a_change(self):
        base = clop_monitor.MonitorSettings()
        for name, value in self.COMPARED.items():
            with self.subTest(field=name):
                edited = clop_monitor.replace(base, **{name: value})
                self.assertNotEqual(clop_monitor.settings_changes(base, edited), ())

    def test_no_field_left_out_of_the_comparison_registers_one(self):
        base = clop_monitor.MonitorSettings()
        for name in self.NOT_COMPARED:
            with self.subTest(field=name):
                edited = clop_monitor.replace(base, **{name: ("something else",)})
                self.assertEqual(clop_monitor.settings_changes(base, edited), ())


class SettingsReloadTests(unittest.TestCase):
    """settings.json is re-read every poll and applied in full or not at all."""

    #: The 4chan thread's own tests aside, no reload here may build a client of its own.
    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("This reload must not build a client")

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "settings.json"
        self.notifier = ReloadNotifier()
        self.built = []
        # No file yet, which is what main pairs with the built-in defaults.
        self.loaded = clop_monitor.LoadedSettings(clop_monitor.MonitorSettings(), None)

    def build_notifier(self, sound):
        self.built.append(sound)
        return ReloadNotifier(sound)

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")

    def load(self, value):
        self.write(value)
        self.loaded = clop_monitor.LoadedSettings(
            load_settings(self.path), clop_monitor.read_settings_source(self.path)
        )
        return self.loaded.settings

    def reload(self, settings, client):
        """The reload as main drives it, with its terminal output captured.

        ``settings`` is named at the call site for readability; the bytes it is paired with
        are whatever this fixture last loaded or last reloaded, exactly as main pairs them.
        """
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.loaded, notifier = clop_monitor.reload_settings(
                self.path,
                clop_monitor.LoadedSettings(settings, self.loaded.source),
                client,
                self.notifier,
                self.build_notifier,
            )
        return self.loaded.settings, notifier, stdout.getvalue()

    @staticmethod
    def _explode_on_parse(path):
        del path
        raise AssertionError("An untouched settings file must not be parsed")

    def test_an_unchanged_file_does_no_work_at_all(self):
        # The point of gating on change detection: a file nobody touched must not rebuild
        # the notifier, spend a request, or say anything.
        settings = self.load({"alerts": {"news": False}})
        client, calls = market_client({}, goods=())
        reloaded, notifier, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertIs(notifier, self.notifier)
        self.assertEqual(self.built, [])
        self.assertEqual(calls, [])
        self.assertEqual(output, "")
        self.assertEqual(self.notifier.failures, [])

    def test_an_unchanged_file_is_never_parsed(self):
        # "No work" has to mean the parse too, not just the setup it gates.
        settings = self.load({"alerts": {"news": False}})
        client, _ = market_client({}, goods=())
        with mock.patch.object(clop_monitor, "load_settings", self._explode_on_parse):
            reloaded, _, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertEqual(output, "")

    def test_an_untouched_file_is_not_re_validated_against_the_world(self):
        # load_settings checks that wav_path still exists, which is a check against the world
        # outside the file rather than against the file. Re-running it every poll turns a WAV
        # on a USB stick, a network share or an on-demand cloud path into a blocking dialog
        # every 60 seconds that nobody caused and that stops the game being read at all until
        # somebody clicks OK. The bytes are the gate precisely because they cannot say
        # "changed" about something outside the file.
        wav = self.path.parent / "alert.wav"
        wav.write_bytes(b"RIFF")
        settings = self.load({"sound": {"wav_path": "alert.wav"}})
        wav.unlink()
        client, calls = market_client({}, goods=())
        reloaded, notifier, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertIs(notifier, self.notifier)
        self.assertEqual(self.notifier.failures, [])
        self.assertEqual(output, "")
        self.assertEqual(calls, [])

    def test_re_saving_the_same_bytes_is_not_an_edit(self):
        settings = self.load({"alerts": {"news": False}})
        self.write({"alerts": {"news": False}})
        client, _ = market_client({}, goods=())
        with mock.patch.object(clop_monitor, "load_settings", self._explode_on_parse):
            reloaded, _, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertEqual(output, "")

    def test_a_cosmetic_edit_is_parsed_once_and_then_left_alone(self):
        # Reindenting changes the bytes but not the settings, so it is parsed and found to
        # change nothing. The new bytes are then remembered, or that one cosmetic edit would
        # be re-parsed on every poll for the rest of the run.
        settings = self.load({"alerts": {"news": False}})
        self.path.write_text(
            json.dumps({"alerts": {"news": False}}, indent=4), encoding="utf-8"
        )
        client, _ = market_client({}, goods=())
        reloaded, _, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertEqual(output, "")
        self.assertEqual(self.notifier.failures, [])
        with mock.patch.object(clop_monitor, "load_settings", self._explode_on_parse):
            self.reload(reloaded, client)

    def test_a_file_that_cannot_be_read_warns_before_any_parse(self):
        settings = self.load({"alerts": {"news": False}})

        def unreadable(path):
            del path
            raise OSError("The process cannot access the file")

        client, _ = market_client({}, goods=())
        with mock.patch.object(clop_monitor.Path, "read_bytes", unreadable):
            reloaded, notifier, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertIs(notifier, self.notifier)
        self.assertEqual(output, "")
        self.assertIn("cannot access", self.notifier.failures[0])
        self.assertIn("still in force", self.notifier.failures[0])

    def test_a_changed_alert_category_is_applied_and_named(self):
        settings = self.load({"alerts": {"news": True}})
        self.write({"alerts": {"news": False}})
        client, calls = market_client({}, goods=())
        reloaded, notifier, output = self.reload(settings, client)
        self.assertFalse(reloaded.alerts.news)
        self.assertEqual(output, "Settings reloaded: alerts.\n")
        self.assertEqual(calls, [])
        self.assertIs(notifier, self.notifier)
        self.assertEqual(self.built, [])

    def test_a_newly_watched_good_re_runs_the_preflight(self):
        settings = self.load({})
        self.write({"market": {"goods": {"Oil": {"alliance": False}}}})
        client, calls = market_client({"buyermarketplace.php": MARKET_FORM}, goods=())
        reloaded, _, output = self.reload(settings, client)
        self.assertEqual(client.market_goods, (("Oil", 1),))
        self.assertEqual([path for path, _ in calls], ["buyermarketplace.php"])
        self.assertEqual(
            output,
            "Settings reloaded: market.goods.\n"
            "Market preflight passed; watching Oil (friends only).\n",
        )
        self.assertEqual([good.name for good in reloaded.alerts.market_goods], ["Oil"])

    def test_a_re_run_preflight_names_the_alliance_it_resolved(self):
        # The README tells a user who joined or left an alliance to touch market.goods so the
        # reload re-resolves it. The resolved alliance is the entire point of that
        # instruction, so the reload has to show it the way startup does.
        settings = self.load({})
        self.write({"market": {"goods": {"Oil": {}}}})
        client, _ = market_client(
            {
                "buyermarketplace.php": MARKET_FORM,
                "index.php": MULTI_NATION_HEADER,
                "viewnation.php?nation_id=12": NATION_PAGE,
            },
            goods=(),
        )
        _, _, output = self.reload(settings, client)
        self.assertEqual(client.alliance_id, 7)
        self.assertEqual(
            output,
            "Settings reloaded: market.goods.\n"
            "Market preflight passed; watching Oil; alliance is The Best Alliance (#7).\n",
        )

    def test_an_unchanged_watch_list_does_not_re_run_the_preflight(self):
        goods = {"Oil": {"alliance": False}}
        settings = self.load({"market": {"goods": goods}, "alerts": {"news": True}})
        self.write({"market": {"goods": goods}, "alerts": {"news": False}})
        client, calls = market_client({}, goods=(("Oil", 1),))
        self.reload(settings, client)
        self.assertEqual(calls, [])
        self.assertEqual(client.market_goods, (("Oil", 1),))

    def test_muting_the_market_category_releases_the_preflight(self):
        # Muting has to release the resolved goods the same way deleting them would, or the
        # monitor keeps POSTing once a poll for orders nothing reads.
        goods = {"Oil": {"alliance": False}}
        settings = self.load({"market": {"goods": goods}})
        self.write({"market": {"goods": goods}, "alerts": {"market_orders": False}})
        client, calls = market_client({}, goods=(("Oil", 1),), alliance_id=7)
        _, _, output = self.reload(settings, client)
        self.assertEqual(client.market_goods, ())
        self.assertIsNone(client.alliance_id)
        self.assertEqual(calls, [])
        self.assertIn("market.goods", output)

    def test_changed_sound_settings_rebuild_the_notifier(self):
        settings = self.load({"sound": {"loop_while_popup_open": False}})
        self.write({"sound": {"loop_while_popup_open": True}})
        client, _ = market_client({}, goods=())
        reloaded, notifier, output = self.reload(settings, client)
        self.assertEqual(self.built, [reloaded.sound])
        self.assertIsNot(notifier, self.notifier)
        self.assertEqual(notifier.sound, reloaded.sound)
        self.assertEqual(output, "Settings reloaded: sound.\n")

    def test_a_reload_that_leaves_the_sound_alone_keeps_the_notifier(self):
        settings = self.load({"alerts": {"news": True}})
        self.write({"alerts": {"news": False}})
        client, _ = market_client({}, goods=())
        _, notifier, _ = self.reload(settings, client)
        self.assertEqual(self.built, [])
        self.assertIs(notifier, self.notifier)

    def test_an_unreadable_file_warns_and_keeps_the_previous_settings(self):
        settings = self.load({"alerts": {"news": False}})
        self.path.unlink()
        self.path.mkdir()
        client, calls = market_client({}, goods=())
        reloaded, notifier, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertIs(notifier, self.notifier)
        self.assertEqual(len(self.notifier.failures), 1)
        self.assertIn("still in force", self.notifier.failures[0])
        self.assertIn("go on polling", self.notifier.failures[0])
        self.assertEqual(output, "")
        self.assertEqual(calls, [])

    def test_a_deleted_file_warns_and_keeps_the_previous_settings(self):
        # An absent file loads cleanly as the built-in defaults, so without this the deletion
        # would silently switch every muted category back on, drop the watched goods, and
        # start writing the state file the user had turned off — under a confirmation line
        # that reads as though the edit took.
        settings = self.load(
            {
                "alerts": {"news": False},
                "cache": {"persist_to_file": False},
                "market": {"goods": {"Oil": {"alliance": False}}},
            }
        )
        self.path.unlink()
        client, calls = market_client({}, goods=(("Oil", 1),))
        reloaded, notifier, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertFalse(reloaded.alerts.news)
        self.assertFalse(reloaded.cache.persist_to_file)
        self.assertEqual([good.name for good in reloaded.alerts.market_goods], ["Oil"])
        self.assertEqual(client.market_goods, (("Oil", 1),))
        self.assertIs(notifier, self.notifier)
        self.assertEqual(self.built, [])
        self.assertEqual(calls, [])
        self.assertEqual(output, "")
        self.assertEqual(len(self.notifier.failures), 1)
        self.assertIn(str(self.path), self.notifier.failures[0])
        self.assertIn("still in force", self.notifier.failures[0])
        self.assertIn("go on polling", self.notifier.failures[0])

    def test_a_monitor_that_never_had_a_settings_file_is_not_warned_about_one(self):
        # Only the transition matters. A monitor started on the built-in defaults is running
        # exactly as intended and must not be interrupted every poll for it.
        settings = load_settings(self.path)
        self.assertFalse(settings.file_found)
        client, calls = market_client({}, goods=())
        reloaded, _, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertEqual(self.notifier.failures, [])
        self.assertEqual(output, "")
        self.assertEqual(calls, [])

    def test_a_settings_file_that_appears_later_is_an_ordinary_reload(self):
        settings = load_settings(self.path)
        self.write({"alerts": {"news": False}})
        client, _ = market_client({}, goods=())
        reloaded, _, output = self.reload(settings, client)
        self.assertFalse(reloaded.alerts.news)
        self.assertEqual(self.notifier.failures, [])
        self.assertEqual(output, "Settings reloaded: alerts.\n")

    def test_an_empty_object_is_how_you_ask_for_the_defaults(self):
        # The refusal above cannot block the legitimate case: writing {} reverts everything
        # to the built-in defaults and is applied like any other edit.
        settings = self.load({"alerts": {"news": False}})
        self.write({})
        client, _ = market_client({}, goods=())
        reloaded, _, output = self.reload(settings, client)
        self.assertTrue(reloaded.alerts.news)
        self.assertEqual(self.notifier.failures, [])
        self.assertEqual(output, "Settings reloaded: alerts.\n")

    def test_a_malformed_file_warns_and_keeps_the_previous_settings(self):
        settings = self.load({"alerts": {"news": False}})
        self.path.write_text("{not json", encoding="utf-8")
        client, _ = market_client({}, goods=())
        reloaded, _, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertIn("Could not read settings file", self.notifier.failures[0])
        self.assertEqual(output, "")

    def test_a_file_that_fails_validation_warns_and_keeps_the_previous_settings(self):
        settings = self.load({"alerts": {"news": False}})
        self.write({"alerts": {"news": "yes please"}})
        client, _ = market_client({}, goods=())
        reloaded, _, _ = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertFalse(reloaded.alerts.news)
        self.assertIn("must be true or false", self.notifier.failures[0])

    def test_an_unresolvable_good_rejects_the_whole_reload(self):
        # The alert half of a file must not land while its market half is refused; "which
        # settings are live" has to stay answerable as one whole file.
        settings = self.load({"alerts": {"news": True}})
        self.write(
            {
                "alerts": {"news": False},
                "market": {"goods": {"Unobtainium": {"alliance": False}}},
            }
        )
        client, _ = market_client({"buyermarketplace.php": MARKET_FORM}, goods=())
        reloaded, _, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertTrue(reloaded.alerts.news)
        self.assertEqual(reloaded.alerts.market_goods, ())
        self.assertEqual(client.market_goods, ())
        self.assertIn("Unobtainium", self.notifier.failures[0])
        self.assertIn("None of it was applied", self.notifier.failures[0])
        self.assertEqual(output, "")

    def _thread(self, thread_id):
        return {"fourchan": {"thread_url": f"https://boards.4chan.org/mlp/thread/{thread_id}"}}

    def test_a_swapped_thread_re_runs_the_preflight_and_baselines_it(self):
        settings = self.load(self._thread(1))
        self.write(self._thread(2))
        baseline = FourChanPost(
            "https://boards.4chan.org/mlp/thread/2", 99, 1700000000, "Anonymous", "hello"
        )
        watched = []

        class PreflightClient:
            def __init__(self, base_url, username, password, fourchan_thread=None, **kwargs):
                del base_url, username, password, kwargs
                watched.append(fourchan_thread)

            def _latest_fourchan_post(self):
                return baseline

        client, _ = market_client({}, goods=())
        with mock.patch.object(clop_monitor, "ClopClient", PreflightClient):
            reloaded, _, output = self.reload(settings, client)
        self.assertEqual([thread.thread_id for thread in watched], [2])
        self.assertEqual(client.fourchan_thread, reloaded.fourchan_thread)
        self.assertEqual(client.fourchan_thread.thread_id, 2)
        self.assertIs(client.initial_fourchan_post, baseline)
        # Adopting post #99 silently decides that everything up to #99 will never alert, so
        # the reload names the baseline in the same words startup does.
        self.assertEqual(
            output,
            "Settings reloaded: fourchan.thread_url.\n"
            "4chan thread preflight passed; latest post is #99.\n",
        )

        # The baseline is adopted rather than alerted on: the poll after the swap compares a
        # post from the new thread against a marker from the old one.
        previous = Snapshot(
            0,
            0,
            None,
            FourChanPost("https://boards.4chan.org/mlp/thread/1", 5, 1, "Anonymous", "old"),
        )
        self.assertEqual(build_alerts(previous, Snapshot(0, 0, None, baseline)), [])

    def test_switching_the_thread_off_stops_watching_without_a_request(self):
        settings = self.load(self._thread(1))
        self.write({"fourchan": {"thread_url": None}})
        client, _ = market_client({}, goods=())
        client.fourchan_thread = settings.fourchan_thread
        with mock.patch.object(clop_monitor, "ClopClient", self.ForbiddenClient):
            reloaded, _, output = self.reload(settings, client)
        self.assertIsNone(reloaded.fourchan_thread)
        self.assertIsNone(client.fourchan_thread)
        self.assertIsNone(client.initial_fourchan_post)
        self.assertIn("fourchan.thread_url", output)

    def test_a_failing_thread_check_leaves_the_market_untouched(self):
        # The all-or-nothing ordering, pinned rather than merely arranged: market_preflight
        # assigns to the client the moment it succeeds, so a reload that changes both
        # sections must not resolve the goods and then discover the thread is unusable.
        # Without this, swapping the two blocks over is a partial apply that passes.
        settings = self.load({})
        self.write(
            {
                "market": {"goods": {"Oil": {"alliance": False}}},
                "fourchan": {"thread_url": "https://boards.4chan.org/mlp/thread/2"},
            }
        )

        class UnreachableThreadClient:
            def __init__(self, *args, **kwargs):
                del args, kwargs

            def _latest_fourchan_post(self):
                raise MonitorError("Could not reach a.4cdn.org")

        client, calls = market_client({"buyermarketplace.php": MARKET_FORM}, goods=())
        with mock.patch.object(clop_monitor, "ClopClient", UnreachableThreadClient):
            reloaded, _, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertEqual(client.market_goods, ())
        self.assertIsNone(client.alliance_id)
        self.assertIsNone(client.fourchan_thread)
        self.assertEqual(calls, [])
        self.assertEqual(output, "")
        self.assertIn("None of it was applied", self.notifier.failures[0])

    def test_a_failing_thread_check_leaves_an_emptied_watch_list_untouched(self):
        # The sibling of the test above, for the other half of the market branch: emptying
        # the watch list clears the client's resolved goods, and that clearing is an apply
        # like any other, so it must not land before a step that can still refuse the reload.
        settings = self.load({"market": {"goods": {"Oil": {"alliance": False}}}})
        self.write(
            {
                "market": {"goods": {}},
                "fourchan": {"thread_url": "https://boards.4chan.org/mlp/thread/2"},
            }
        )

        class UnreachableThreadClient:
            def __init__(self, *args, **kwargs):
                del args, kwargs

            def _latest_fourchan_post(self):
                raise MonitorError("Could not reach a.4cdn.org")

        client, calls = market_client({}, goods=(("Oil", 1),), alliance_id=7)
        with mock.patch.object(clop_monitor, "ClopClient", UnreachableThreadClient):
            reloaded, _, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertEqual(client.market_goods, (("Oil", 1),))
        self.assertEqual(client.alliance_id, 7)
        self.assertIsNone(client.fourchan_thread)
        self.assertEqual(calls, [])
        self.assertEqual(output, "")
        self.assertIn("None of it was applied", self.notifier.failures[0])

    def test_a_newly_configured_archived_thread_is_a_rejected_reload(self):
        # A thread that is already archived when you name it is a typo in a text file, not
        # the game telling a running watch that its job is over.
        settings = self.load({"alerts": {"news": True}})
        broken = dict(self._thread(2))
        broken["alerts"] = {"news": False}
        self.write(broken)

        class ArchivedClient:
            def __init__(self, *args, **kwargs):
                del args, kwargs

            def _latest_fourchan_post(self):
                raise ArchivedThreadError("Configured 4chan thread is archived")

        client, _ = market_client({}, goods=())
        with mock.patch.object(clop_monitor, "ClopClient", ArchivedClient):
            reloaded, _, output = self.reload(settings, client)
        self.assertIs(reloaded, settings)
        self.assertTrue(reloaded.alerts.news)
        self.assertIsNone(client.fourchan_thread)
        self.assertIn("archived", self.notifier.failures[0])
        self.assertIn("go on polling", self.notifier.failures[0])
        self.assertEqual(output, "")


class ReloadApplyRegionTests(unittest.TestCase):
    """Nothing that changes the client may sit in reload_settings' fallible block.

    A behavioural test can only catch a mutation placed *before* whichever step raises; one
    placed after it is invisible until somebody reorders the block, at which point it becomes
    the partial apply the whole design forbids. Reading the function's own syntax tree pins
    the rule rather than one arrangement of it: the try is for work that can fail, and every
    apply lives below it.
    """

    def fallible_block(self):
        """The try that brings a changed section into service."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(clop_monitor.reload_settings)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Try) and "market_preflight" in ast.dump(node):
                return node
        raise AssertionError("reload_settings has no block that runs the market preflight")

    def test_the_fallible_block_assigns_nothing_to_the_client(self):
        assigned = []
        for node in ast.walk(self.fallible_block()):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            else:
                continue
            assigned.extend(
                target.attr
                for target in targets
                if isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "client"
            )
        self.assertEqual(
            assigned,
            [],
            "reload_settings assigns to the client inside the block that can fail. Move it "
            "below the except, with the other applies: a reload refused after that "
            "assignment has already half-applied itself.",
        )

    def test_the_only_client_call_that_can_fail_is_the_atomic_one(self):
        # market_preflight may sit there because it assigns its two fields together at the
        # end or not at all; a second fallible client call would have no such guarantee.
        called = [
            node.func.attr
            for node in ast.walk(self.fallible_block())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "client"
        ]
        self.assertEqual(called, ["market_preflight"])


class PollingClient:
    """A client that logs in, watches nothing, and sees two unread messages every poll."""

    base_url = "https://4clop.org/"

    def __init__(self, *args, **kwargs):
        del args
        self.fourchan_thread = kwargs.get("fourchan_thread")
        self.initial_fourchan_post = kwargs.get("initial_fourchan_post")
        self.market_goods = ()
        self.alliance_id = None

    def login(self):
        return None

    def market_preflight(self, goods):
        del goods
        return None

    def snapshot(self, include_market=True):
        del include_market
        return Snapshot(2, 0, None)


class SettingsReloadThroughMainTests(unittest.TestCase):
    """The reload as the polling loop drives it, end to end through main()."""

    def _record_notifiers(self):
        """Replace Notifier so a test can read every dialog, alert and rebuild."""
        built = []

        class RecordingNotifier(Notifier):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.messages = []
                self.failures = []
                built.append(self)

            def notify(self, message):
                self.messages.append(message)
                return False

            def notify_failure(self, message):
                # These tests run without CLOP_NATION, so the "Sheet sync is OFF" startup dialog
                # fires every time and is not what any of them is about. SheetSyncOffTests covers
                # it directly.
                if "Sheet sync is OFF" not in message:
                    self.failures.append(message)
                return True

        patcher = mock.patch.object(clop_monitor, "Notifier", RecordingNotifier)
        patcher.start()
        self.addCleanup(patcher.stop)
        return built

    def _run(self, directory, settings_value, sleep, client=PollingClient, env=None):
        """Drive main() against a scripted client, with ``sleep`` editing between polls.

        ``sleep`` stands in for the poll boundary, which works because the loop calls it
        exactly once per iteration. Adding a second sleep anywhere in that loop — a retry
        backoff, say — would silently renumber the polls these tests count, so change the
        counting here rather than trusting the numbers if that ever happens.
        """
        path = Path(directory) / "settings.json"
        path.write_text(json.dumps(settings_value), encoding="utf-8")
        environment = {"CLOP_PASSWORD": "secret"}
        environment.update(env or {})
        with mock.patch.object(clop_monitor, "ClopClient", client), mock.patch.object(
            clop_monitor.time, "sleep", sleep
        ), mock.patch.dict(clop_monitor.os.environ, environment):
            return path, main(
                [
                    "--settings",
                    str(path),
                    "--env-file",
                    str(Path(directory) / "absent.env"),
                    "--username",
                    "tester",
                    "--state",
                    str(Path(directory) / "state.json"),
                    "--no-desktop-notifications",
                ]
            )

    def test_a_changed_alert_category_takes_effect_on_the_next_poll(self):
        notifiers = self._record_notifiers()
        quiet = {"alerts": {"user_messages": False}, "cache": {"persist_to_file": False}}
        polls = []

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            path = Path(directory) / "settings.json"

            def sleep(seconds):
                del seconds
                polls.append(1)
                if len(polls) == 1:
                    path.write_text(json.dumps(quiet), encoding="utf-8")
                else:
                    raise KeyboardInterrupt

            _, code = self._run(
                directory,
                {"alerts": {"user_messages": True}, "cache": {"persist_to_file": False}},
                sleep,
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(polls), 2)
        # The bootstrap notifier is built before the settings are read; the real one is next.
        self.assertEqual(len(notifiers[1].messages), 1)
        self.assertIn("unread user message", notifiers[1].messages[0])

    def test_a_broken_file_warns_every_poll_and_keeps_polling(self):
        # A monitor running on settings you think you replaced is worth interrupting more
        # than once, so the warning repeats for as long as the file stays broken.
        notifiers = self._record_notifiers()
        polls = []

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            path = Path(directory) / "settings.json"

            def sleep(seconds):
                del seconds
                polls.append(1)
                if len(polls) == 1:
                    path.write_text("{not json", encoding="utf-8")
                elif len(polls) == 3:
                    raise KeyboardInterrupt

            _, code = self._run(directory, {"cache": {"persist_to_file": False}}, sleep)
        self.assertEqual(code, 0)
        self.assertEqual(len(polls), 3)
        failures = notifiers[1].failures
        self.assertEqual(len(failures), 2)
        for failure in failures:
            self.assertIn("go on polling", failure)
        # Polling never stopped: every iteration still read the game.
        self.assertEqual(len(notifiers[1].messages), 3)

    def test_a_deleted_file_warns_and_reverts_nothing(self):
        # The state file is the visible half of the harm: a silent revert would flip
        # cache.persist_to_file back on and start writing a file the user had turned off.
        notifiers = self._record_notifiers()
        polls = []

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            state = Path(directory) / "state.json"
            path = Path(directory) / "settings.json"

            def sleep(seconds):
                del seconds
                polls.append(1)
                if len(polls) == 1:
                    path.unlink()
                elif len(polls) == 3:
                    raise KeyboardInterrupt

            _, code = self._run(
                directory,
                {"alerts": {"user_messages": False}, "cache": {"persist_to_file": False}},
                sleep,
            )
            state_written = state.exists()
        self.assertEqual(code, 0)
        self.assertEqual(len(polls), 3)
        self.assertFalse(state_written)
        # Polling continued, and the muted category stayed muted rather than reverting.
        self.assertEqual(notifiers[1].messages, [])
        self.assertEqual(len(notifiers[1].failures), 2)
        for failure in notifiers[1].failures:
            self.assertIn("still in force", failure)

    def test_a_rebuilt_notifier_is_the_one_that_handles_the_next_alert(self):
        # reload_settings returns the notifier rather than mutating one, so main has to bind
        # what it returns. Dropping that half of the two-value return would report a sound
        # edit as applied and silently keep alerting through the old notifier.
        notifiers = self._record_notifiers()
        polls = []

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            path = Path(directory) / "settings.json"

            def sleep(seconds):
                del seconds
                polls.append(1)
                if len(polls) == 1:
                    path.write_text(
                        json.dumps(
                            {
                                "cache": {"persist_to_file": False},
                                "sound": {"wav_path": None, "loop_while_popup_open": True},
                            }
                        ),
                        encoding="utf-8",
                    )
                else:
                    raise KeyboardInterrupt

            _, code = self._run(
                directory,
                {
                    "cache": {"persist_to_file": False},
                    "sound": {"wav_path": None, "loop_while_popup_open": False},
                },
                sleep,
                env={"CLOP_WEBHOOK_URL": "https://example.invalid/hook"},
            )
        self.assertEqual(code, 0)
        self.assertEqual(len(polls), 2)
        # The bootstrap notifier, the real one, then the rebuilt one.
        self.assertEqual(len(notifiers), 3)
        started, rebuilt = notifiers[1], notifiers[2]
        self.assertTrue(rebuilt.sound.loop_while_popup_open)
        self.assertEqual(len(started.messages), 1)
        self.assertEqual(len(rebuilt.messages), 1)
        # A rebuild replaces the sound and nothing else: the webhook and the desktop setting
        # are process-level and must survive it.
        self.assertEqual(rebuilt.webhook_url, started.webhook_url)
        self.assertEqual(rebuilt.webhook_url, "https://example.invalid/hook")
        self.assertEqual(rebuilt.desktop, started.desktop)

    def test_an_edit_landing_during_the_startup_parse_is_not_missed(self):
        # The bytes are read before the parse, so the bytes main remembers can only be older
        # than or the same age as the settings they are paired with. Reversed, an edit landing
        # in that window would be remembered as already loaded and would never take effect --
        # silently skipping a real change, which is worse than the redundant re-parse this
        # ordering costs instead. Nothing but this ordering prevents it.
        notifiers = self._record_notifiers()
        muted = {"alerts": {"user_messages": False}, "cache": {"persist_to_file": False}}
        parsed = []
        real_load_settings = clop_monitor.load_settings

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(stdout):
            path = Path(directory) / "settings.json"

            def edit_after_parsing(settings_path):
                # The file changes the instant the startup parse has read it, which is the
                # window the ordering exists for.
                result = real_load_settings(settings_path)
                if not parsed:
                    parsed.append(True)
                    path.write_text(json.dumps(muted), encoding="utf-8")
                return result

            def sleep(seconds):
                del seconds
                raise KeyboardInterrupt

            with mock.patch.object(clop_monitor, "load_settings", edit_after_parsing):
                _, code = self._run(
                    directory,
                    {"alerts": {"user_messages": True}, "cache": {"persist_to_file": False}},
                    sleep,
                )
        self.assertEqual(code, 0)
        # The edit was picked up before the first check, so the muted category never alerted.
        self.assertIn("Settings reloaded: alerts.", stdout.getvalue())
        self.assertEqual(notifiers[1].messages, [])

    def test_enabling_the_file_cache_mid_run_writes_without_reading(self):
        # Pinning today's behaviour rather than endorsing it: load_snapshot runs once, before
        # the loop, so switching cache.persist_to_file on mid-run starts writing the state
        # file while the baseline in play stays the in-memory one.
        self._record_notifiers()
        polls = []
        loads = []
        real_load_snapshot = clop_monitor.load_snapshot

        def counting_load_snapshot(path):
            loads.append(path)
            return real_load_snapshot(path)

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(
            io.StringIO()
        ), mock.patch.object(clop_monitor, "load_snapshot", counting_load_snapshot):
            state = Path(directory) / "state.json"
            path = Path(directory) / "settings.json"

            def sleep(seconds):
                del seconds
                polls.append(1)
                if len(polls) == 1:
                    self.assertFalse(state.exists())
                    path.write_text(
                        json.dumps({"cache": {"persist_to_file": True}}), encoding="utf-8"
                    )
                else:
                    raise KeyboardInterrupt

            _, code = self._run(directory, {"cache": {"persist_to_file": False}}, sleep)
            state_written = state.exists()
        self.assertEqual(code, 0)
        self.assertTrue(state_written)
        self.assertEqual(len(loads), 0)

    def test_the_startup_thread_baseline_line_names_the_post(self):
        # The literal that reload output is pinned against, so the two cannot drift apart
        # into two differently worded sentences about the same decision.
        self._record_notifiers()

        class ThreadClient(PollingClient):
            def _latest_fourchan_post(self):
                return FourChanPost(
                    "https://boards.4chan.org/mlp/thread/1", 99, 1, "Anonymous", "hi"
                )

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(stdout):

            def sleep(seconds):
                del seconds
                raise KeyboardInterrupt

            self._run(
                directory,
                {
                    "cache": {"persist_to_file": False},
                    "fourchan": {"thread_url": "https://boards.4chan.org/mlp/thread/1"},
                },
                sleep,
                client=ThreadClient,
            )
        self.assertIn(
            "4chan thread preflight passed; latest post is #99.", stdout.getvalue()
        )

    def test_a_thread_that_archives_while_watched_still_stops_the_monitor(self):
        notifiers = self._record_notifiers()

        class ArchivingClient(PollingClient):
            def _latest_fourchan_post(self):
                return FourChanPost(
                    "https://boards.4chan.org/mlp/thread/1", 5, 1, "Anonymous", "hi"
                )

            def snapshot(self, include_market=True):
                del include_market
                raise ArchivedThreadError(
                    "Configured 4chan thread is archived and cannot receive new posts"
                )

        def unexpected_sleep(seconds):
            del seconds
            raise AssertionError("An archived thread must stop the monitor, not retry")

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            _, code = self._run(
                directory,
                {
                    "cache": {"persist_to_file": False},
                    "fourchan": {"thread_url": "https://boards.4chan.org/mlp/thread/1"},
                },
                unexpected_sleep,
                client=ArchivingClient,
            )
        self.assertEqual(code, 1)
        self.assertIn("archived", notifiers[1].failures[0])
        self.assertIn("The monitor has stopped", notifiers[1].failures[0])


class ChannelFailureTests(unittest.TestCase):
    """A broken notification channel must be announced on the other one, never only in the terminal.

    This is the failure a user cannot detect unaided: alerts stop arriving, and silence looks
    exactly like nothing having happened.
    """

    def test_dead_webhook_raises_a_dialog(self):
        notifier = clop_monitor.Notifier(desktop=True, webhook_url="https://hook.test/x")
        dialogs = []
        notifier._desktop_notification = lambda message, title=None, error=False: (
            dialogs.append(message) or True
        )
        with mock.patch.object(
            clop_monitor.urllib.request, "urlopen",
            side_effect=urllib.error.URLError("refused"),
        ), contextlib.redirect_stderr(io.StringIO()):
            notifier._webhook_notification("anything")
        self.assertEqual(len(dialogs), 1)
        self.assertIn("webhook is not receiving alerts", dialogs[0])

    def test_broken_dialog_goes_to_the_webhook(self):
        notifier = clop_monitor.Notifier(desktop=True, webhook_url="https://hook.test/x")
        sent = []
        notifier._webhook_notification = sent.append
        with contextlib.redirect_stderr(io.StringIO()):
            notifier._report_channel_failure("dialogs are broken", via_desktop=False)
        self.assertEqual(sent, ["dialogs are broken"])

    def test_both_channels_down_does_not_recurse(self):
        notifier = clop_monitor.Notifier(desktop=True, webhook_url="https://hook.test/x")
        calls = []

        def dead_webhook(message):
            calls.append(("webhook", message))
            notifier._report_channel_failure("webhook died too", via_desktop=True)

        def dead_dialog(message, title=None, error=False):
            calls.append(("dialog", message))
            notifier._report_channel_failure("dialog died too", via_desktop=False)
            return False

        notifier._webhook_notification = dead_webhook
        notifier._desktop_notification = dead_dialog
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            notifier._report_channel_failure("start", via_desktop=False)
        # One hop to the other channel and no further: the fallback of the fallback would be the
        # channel we already know is dead. Both messages still reach stderr, which by then is
        # genuinely all that is left.
        self.assertEqual([kind for kind, _ in calls], ["webhook"])
        self.assertIn("start", stderr.getvalue())
        self.assertIn("webhook died too", stderr.getvalue())


class NoTerminalOnlyFailuresTests(unittest.TestCase):
    """No failure path may report only to a terminal.

    Nobody watches the terminal. A failure that prints and nothing else looks identical to the
    tool working, which is the single worst outcome available to a monitor: the user believes they
    are being told about problems while they are not. Every failure gets the blocking dialog --
    ``Notifier.notify_failure`` in the monitor, ``popup_failure`` in the hand-run scripts.

    The two allowed exceptions are the fallbacks inside ``_report_channel_failure`` itself, which
    run only when a notification channel is already known to be broken.
    """

    FILES = ("clop_monitor.py", "buildings.py", "stockpiles.py", "sheets.py", "overview.py")

    #: The only functions allowed to write a failure to stderr without a dialog behind it, each
    #: with the reason it cannot use one. Adding to this list should feel uncomfortable.
    ALLOWED = {
        # These *are* the dialog and the fallback machinery. They print as well as notify.
        "notify": "prints alongside raising the dialog",
        "notify_failure": "prints alongside raising the dialog",
        "_report_channel_failure": "runs only when a channel is already broken; prints last",
        # Argument validation, before any Notifier exists and while the user is at the prompt
        # they just typed into. argparse's own errors are terminal-only regardless.
        "main": "usage error at startup, exits immediately in front of the user",
    }

    def test_no_failure_prints_without_a_dialog(self):
        unexpected = []
        for name in self.FILES:
            source = Path(__file__).resolve().parent / name
            tree = ast.parse(source.read_text(encoding="utf-8"))
            # Every print in the file, labelled with the function it sits in -- or "<module>" for
            # the `if __name__ == "__main__":` blocks, which is exactly where the scripts' failure
            # handlers live and where an earlier version of this test missed them entirely.
            owner = {}
            for parent in ast.walk(tree):
                if isinstance(parent, ast.FunctionDef):
                    for node in ast.walk(parent):
                        owner.setdefault(id(node), parent.name)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "print"):
                    continue
                where = owner.get(id(node), "<module>")
                if where in self.ALLOWED:
                    continue
                if any(kw.arg == "file" and ast.unparse(kw.value).endswith("stderr")
                       for kw in node.keywords):
                    unexpected.append(f"{name}:{node.lineno} in {where}")
        self.assertEqual(
            unexpected, [],
            "these report a failure to stderr with no dialog behind it: " + ", ".join(unexpected),
        )


class StartupSheetCheckTests(SettingsReloadThroughMainTests):
    """What the startup tab check does to the rest of the session, driven through main().

    This is the gap that let a single passing Google timeout switch sheet sync off for a whole
    run: the startup check caught every SheetError alike and set sync_sheet = None, so the monitor
    went on looking perfectly healthy while the tab silently went stale. The old test for this
    inspected main()'s source text for a string, which could never have noticed.
    """

    def _drive(self, require_tab_raises, polls_wanted=2):
        """Run main() with CLOP_NATION set and a GoogleSheet whose require_tab misbehaves."""
        import sheets

        synced = []

        class Sheet:
            def __init__(self, *a, **k):
                pass

            def require_tab(self, tab):
                if require_tab_raises is not None:
                    raise require_tab_raises

        def fake_step(*args, **kwargs):
            synced.append(args[2])       # the nation
            return "digest"

        notifiers = self._record_notifiers()
        polls = []

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            def sleep(seconds):
                del seconds
                polls.append(1)
                if len(polls) >= polls_wanted:
                    raise KeyboardInterrupt

            with mock.patch.object(sheets, "GoogleSheet", Sheet), mock.patch.object(
                clop_monitor, "sync_sheet_step", fake_step
            ):
                _, code = self._run(
                    directory,
                    {"cache": {"persist_to_file": False}},
                    sleep,
                    env={"CLOP_NATION": "LePone(Z)"},
                )
        self.assertEqual(code, 0)
        return notifiers[1].failures, synced

    def test_a_timeout_at_startup_leaves_sheet_sync_running(self):
        # The production failure, exactly: "timed out reading the sheet endpoint's reply after 2
        # attempts in 45s". Weather. It must not cost the session its sheet sync.
        from sheets import SheetError

        failures, synced = self._drive(
            SheetError("timed out reading the sheet endpoint's reply after 2 attempts in 45s")
        )
        self.assertTrue(synced, "sheet sync was switched off by a passing outage")
        self.assertTrue(any("stays ON" in f for f in failures))
        self.assertFalse(any("Sheet sync is off" in f for f in failures))

    def test_a_missing_tab_at_startup_switches_sheet_sync_off(self):
        # Definitive: it will still be missing next minute, and somebody has to go and fix it.
        from sheets import SheetTabMissing

        failures, synced = self._drive(SheetTabMissing("nation tab 'Ghost' does not exist"))
        self.assertEqual(synced, [])
        self.assertTrue(any("Sheet sync is off" in f for f in failures))

    def test_a_clean_startup_check_syncs_every_poll(self):
        _failures, synced = self._drive(None, polls_wanted=3)
        self.assertEqual(synced, ["LePone(Z)"] * 3)


class MissingTabDuringPollingTests(SettingsReloadThroughMainTests):
    def test_a_missing_tab_found_while_polling_switches_sync_off_once(self):
        # Otherwise it is an identical blocking dialog every 60 seconds, forever.
        import sheets

        calls = []

        class Sheet:
            def __init__(self, *a, **k):
                pass

            def require_tab(self, tab):
                return None

        def fake_step(*args, **kwargs):
            calls.append(1)
            raise sheets.SheetTabMissing("no such tab: Ghost")

        notifiers = self._record_notifiers()
        polls = []

        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            def sleep(seconds):
                del seconds
                polls.append(1)
                if len(polls) >= 4:
                    raise KeyboardInterrupt

            with mock.patch.object(sheets, "GoogleSheet", Sheet), mock.patch.object(
                clop_monitor, "sync_sheet_step", fake_step
            ):
                _, code = self._run(
                    directory,
                    {"cache": {"persist_to_file": False}},
                    sleep,
                    env={"CLOP_NATION": "LePone(Z)"},
                )
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1, "sync kept running after a definitive tab failure")
        off = [f for f in notifiers[1].failures if "Sheet sync is off" in f]
        self.assertEqual(len(off), 1)


class SheetSyncOffTests(unittest.TestCase):
    def test_missing_nation_raises_a_dialog_not_a_terminal_line(self):
        """Sheet sync silently doing nothing is indistinguishable from it working.

        With CLOP_NATION unset the monitor still polls, still alerts, and never writes a single
        cell. The only symptom is a W10 that stopped advancing, which nobody sees until they need
        it -- so the user is told at startup, in a dialog.
        """
        source = inspect.getsource(clop_monitor.main)
        start = source.index("nation_from_env(")
        branch = source[start:start + 600]
        self.assertIn("notify_failure", branch)
        self.assertIn("Sheet sync is OFF", branch)
        self.assertNotIn('print("Sheet sync off', branch)


class OpenTransportFailureTests(unittest.TestCase):
    """Every way a fetch can fail must arrive as MonitorError, which the callers know to report.

    Anything else escapes their handlers and kills the monitor with a traceback and no dialog --
    the one outcome this tool must never produce, since a silent death looks exactly like a quiet
    game.
    """

    def _client_raising(self, error):
        client = clop_monitor.ClopClient("https://example.test/", "u", "p")

        class Opener:
            def open(self, request, timeout=None):
                raise error

        client.opener = Opener()
        return client

    def test_incomplete_read_becomes_a_monitor_error(self):
        # A response that dies mid-body. IncompleteRead is an HTTPException, and is neither an
        # HTTPError nor a URLError, so it used to escape uncaught.
        client = self._client_raising(http.client.IncompleteRead(b"half a page"))
        with self.assertRaises(clop_monitor.MonitorError) as caught:
            client._open("overview.php")
        self.assertIn("broke off part-way", str(caught.exception))

    def test_body_read_timeout_becomes_a_monitor_error(self):
        class Headers:
            def get_content_charset(self):
                return None

        class TimedOutResponse:
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return None

            def read(self):
                raise TimeoutError("The read operation timed out")

        class Opener:
            def open(self, request, timeout=None):
                del request, timeout
                return TimedOutResponse()

        client = clop_monitor.ClopClient("https://example.test/", "u", "p")
        client.opener = Opener()
        with self.assertRaises(clop_monitor.MonitorError) as caught:
            client._open("reports.php")
        self.assertIn(
            "Timed out while reading https://example.test/reports.php",
            str(caught.exception),
        )

    def test_other_http_exceptions_become_a_monitor_error(self):
        client = self._client_raising(http.client.BadStatusLine("garbage"))
        with self.assertRaises(clop_monitor.MonitorError):
            client._open("overview.php")

    def test_url_error_still_becomes_a_monitor_error(self):
        client = self._client_raising(urllib.error.URLError("no route"))
        with self.assertRaises(clop_monitor.MonitorError) as caught:
            client._open("overview.php")
        self.assertIn("Could not reach", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
