import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import clop_monitor
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
    report_is_ignored,
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
            ("You gained 3 Pies. Done.", "2026-08-17 08:02:33"),
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

            def snapshot(self):
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
            def snapshot(self):
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
        self.assertTrue(report_is_ignored("Report #42 filed", settings.alerts.report_ignore))

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


#: Each shipped example pattern with a report message it is meant to silence.
IGNORABLE_REPORTS = [
    (
        "You sold % and made % bits.",
        "You sold 10 Copper to quaity kirenese merch and ice and made 9,000 bits.",
    ),
    (
        "You bought % from % for % bits.",
        "You bought 50 Apples from Luna Sueno for 55,000 bits.",
    ),
    (
        "Change in Satisfaction:",
        "Change in Satisfaction: +2 Change in GDP: +1,200,000 bits",
    ),
    ("Burn Oil", "Burn Oil consumed 5 barrels"),
    ("Distribute Pies", "Distribute Pies to 3 cities"),
    (
        "Build % completed successfully.",
        "Build Advanced Factory completed successfully.",
    ),
]
WORTH_ALERTING = "Your nation was attacked by Sombra and lost 3 Tanks."


class ReportIgnorePatternTests(unittest.TestCase):
    """Patterns match anywhere in a report; % stands for any run of characters."""

    def test_each_example_pattern_ignores_its_own_report(self):
        for pattern, message in IGNORABLE_REPORTS:
            with self.subTest(pattern=pattern):
                self.assertTrue(report_is_ignored(message, [pattern]))

    def test_no_example_pattern_ignores_a_report_worth_seeing(self):
        patterns = [pattern for pattern, _ in IGNORABLE_REPORTS]
        self.assertFalse(report_is_ignored(WORTH_ALERTING, patterns))

    def test_a_buy_pattern_does_not_ignore_a_sale(self):
        sale = "You sold 10 Copper to quaity kirenese merch and ice and made 9,000 bits."
        self.assertFalse(report_is_ignored(sale, ["You bought % from % for % bits."]))

    def test_matching_ignores_case(self):
        self.assertTrue(report_is_ignored("BURN OIL consumed 5 barrels", ["burn oil"]))

    def test_a_wildcard_spans_any_text_including_none(self):
        self.assertTrue(
            report_is_ignored(
                "Build Really Very Large Ovipositor Factory completed successfully.",
                ["Build % completed successfully."],
            )
        )
        self.assertTrue(
            report_is_ignored("Build  completed successfully.", ["Build % completed successfully."])
        )

    def test_a_wildcard_may_start_or_end_a_pattern(self):
        self.assertTrue(report_is_ignored("You made 9,000 bits.", ["% bits."]))

    def test_no_patterns_ignores_nothing(self):
        self.assertFalse(report_is_ignored(WORTH_ALERTING, []))

    def test_shipped_patterns_are_present_but_all_commented_out(self):
        value = json.loads(Path("settings.example.json").read_text(encoding="utf-8-sig"))
        self.assertEqual(
            value["reports"]["ignore"],
            [f"# {pattern}" for pattern, _ in IGNORABLE_REPORTS],
        )
        self.assertNotIn("_ignore_examples", value["reports"])
        # Shipped as-is, the example silences nothing.
        self.assertEqual(load_settings(Path("settings.example.json")).alerts.report_ignore, ())

    def test_uncommenting_a_shipped_pattern_switches_it_on(self):
        value = json.loads(Path("settings.example.json").read_text(encoding="utf-8-sig"))
        value["reports"]["ignore"] = [
            entry[2:] if "Burn Oil" in entry else entry for entry in value["reports"]["ignore"]
        ]
        # The bundled WAV is reached by a path relative to the settings file, which a temp
        # directory has no copy of; the sound is irrelevant here.
        value["sound"]["wav_path"] = None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            settings = load_settings(path)
        self.assertEqual(settings.alerts.report_ignore, ("Burn Oil",))
        self.assertTrue(report_is_ignored("Burn Oil consumed 5 barrels", settings.alerts.report_ignore))


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
            [(message, "2026-08-17 08:0%d:00" % (index + 1)) for index, (_, message) in enumerate(IGNORABLE_REPORTS)]
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

            def snapshot(self):
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

            def snapshot(self):
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
            def snapshot(self):
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
        """Replace the notifier so tests can read what the monitor announced."""
        failures = []

        class RecordingNotifier(Notifier):
            def notify(self, message):
                return False

            def notify_failure(self, message):
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

            def snapshot(self):
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
            (clop_monitor.WatchedGood("Machinery Parts", True, True, (), ()),),
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
                    }
                }
            }
        )
        self.assertEqual(
            settings.alerts.market_goods,
            (clop_monitor.WatchedGood("Oil", False, True, ("Luna Sueno",), ("Sombra",)),),
        )

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
        with self.assertRaisesRegex(MonitorError, "alliance, always, friends, never"):
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
    def example(self):
        return json.loads(Path("settings.example.json").read_text(encoding="utf-8-sig"))

    def test_every_tradeable_good_ships_commented_out(self):
        goods = self.example()["market"]["goods"]
        self.assertEqual(list(goods), [f"# {name}" for name in TRADEABLE_GOODS])

    def test_every_shipped_good_shows_all_four_knobs(self):
        for key, good in self.example()["market"]["goods"].items():
            with self.subTest(good=key):
                self.assertEqual(
                    sorted(good), ["alliance", "always", "friends", "never"]
                )
                self.assertTrue(good["friends"])
                self.assertTrue(good["alliance"])
                self.assertEqual(good["always"], [])
                self.assertEqual(good["never"], [])

    def test_shipped_as_is_the_example_watches_nothing(self):
        settings = load_settings(Path("settings.example.json"))
        self.assertEqual(settings.alerts.market_goods, ())
        self.assertTrue(settings.alerts.market_orders)

    def test_uncommenting_a_shipped_good_switches_it_on(self):
        value = self.example()
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


if __name__ == "__main__":
    unittest.main()
