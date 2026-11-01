"""Exercise the active scan routine without sending any real mouse input."""
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch

import numpy as np
import bot_core as bot


class BuilderScanLimits(TestCase):
    def run_scan(self, limit, already_at_top):
        logs, delays = [], []
        clock = {"now": 0.0, "last_scroll": None}
        def sleep(seconds):
            delays.append(seconds)
            clock["now"] += seconds
            return True
        def wheel(*args, **kwargs):
            clock["last_scroll"] = clock["now"]
        def read_settled_page(*args):
            if clock["last_scroll"] is not None:
                self.assertGreaterEqual(clock["now"] - clock["last_scroll"], 1.0)
            return []
        panel = {"open": True}
        frame = np.zeros((730, 1300, 3), dtype=np.uint8)
        mouse = SimpleNamespace(moveTo=Mock(), scroll=Mock(), click=Mock())
        mouse.scroll.side_effect = wheel
        mouse.click.side_effect = lambda *args, **kwargs: panel.update(open=False)
        read = Mock(return_value=[(None, "Suggested upgrades")] if already_at_top else [])
        app = SimpleNamespace(
            bot_running=True,
            capture_screen=lambda rect: frame,
            run_ocr_on_region=read,
            get_scroll_page_limit=lambda: limit,
            write_terminal=logs.append,
            update_gui_metrics=Mock(),
            bot_sleep=sleep,
        )
        with patch.object(bot, "pyautogui", mouse), \
             patch.object(bot, "ocr", return_value=(None, None)), \
             patch.object(bot, "builder_sidebar_visible", side_effect=lambda *args: panel["open"]), \
             patch.object(bot, "village_builder_anchor", return_value=(0.93, 0.82)), \
             patch.object(bot, "run_layout_independent_builder_ocr", side_effect=read_settled_page) as read_page, \
             patch.object(bot.time, "sleep"):
            result = bot.FancyCoCBotGUI.find_and_upgrade_walls(app, (0, 0, 1300, 730), 0, 0)
        self.assertFalse(result)
        self.assertEqual(read_page.call_count, 2 * limit)
        self.assertEqual(sum("[Upgrades Scan]: Page" in line for line in logs), limit)
        self.assertTrue(any(f"up to {limit} pages" in line for line in logs))
        self.assertFalse(any("60 pages" in line for line in logs))
        down = [call for call in mouse.scroll.call_args_list if call.args[0] < 0]
        up = [call for call in mouse.scroll.call_args_list if call.args[0] > 0]
        self.assertEqual(len(down), 4 * (limit - 1))
        self.assertEqual(len(up), 0 if already_at_top else 4 * limit)
        self.assertTrue(all(call.args[0] == -960 for call in down))
        self.assertTrue(all(call.args[0] == 960 for call in up))
        self.assertTrue(all(call.kwargs.get("_pause") is False for call in mouse.scroll.call_args_list))
        self.assertEqual(delays.count(0.35), limit)

    def test_setting_controls_exact_page_count(self):
        for limit in (1, 3, 7, 20):
            with self.subTest(limit=limit):
                self.run_scan(limit, already_at_top=True)

    def test_reset_is_bounded_by_same_setting(self):
        for limit in (1, 3):
            with self.subTest(limit=limit):
                self.run_scan(limit, already_at_top=False)

    def test_installed_windows_backend_uses_raw_wheel_delta(self):
        # Intercept the native event helper: never send real desktop input.
        from pyautogui import _pyautogui_win as backend
        with patch.object(backend, "_position", return_value=(500, 500)), \
             patch.object(backend, "_size", return_value=(1920, 1080)), \
             patch.object(backend, "_sendMouseEvent") as send:
            backend._scroll(-960, 500, 500)
        self.assertEqual(send.call_args.kwargs["dwData"], -960)
        self.assertEqual(abs(send.call_args.kwargs["dwData"]) // 120, 8)


if __name__ == "__main__":
    main()
