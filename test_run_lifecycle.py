"""Start/Stop concurrency regressions without Tk or desktop input."""
import threading
from unittest import TestCase, main
from unittest.mock import Mock, patch
import bot_core as bot

class RunLifecycle(TestCase):
    def app(self):
        app=object.__new__(bot.FancyCoCBotGUI)
        app.bot_running=False
        app.license_check=None
        app.root=Mock()
        app.btn_toggle=Mock()
        app.write_terminal=Mock()
        app.txt_min_gold=Mock()
        app.txt_min_elixir=Mock()
        app.enable_walls_var=Mock()
        app.enable_walls_var.get.return_value=False
        app.clean_input_string=Mock()
        return app

    def test_stop_start_during_inflight_work_does_not_revive_worker(self):
        app=self.app()
        entered,release=threading.Event(),threading.Event()
        continued=[]
        def work():
            entered.set()
            release.wait(5)
            continued.append(app.bot_running)
        app.core_bot_loop=Mock(side_effect=work)
        with patch.object(bot.pyautogui,'press') as press:
            app.toggle_bot_engine()
            worker=app.bot_thread
            self.assertTrue(entered.wait(2))
            try:
                app.toggle_bot_engine() # stop
                app.toggle_bot_engine() # immediate restart must be rejected
                self.assertIs(app.bot_thread,worker)
                self.assertFalse(app.bot_running)
                self.assertEqual(app.core_bot_loop.call_count,1)
                press.assert_not_called() # stop must not disturb the game menu
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertEqual(continued,[False])
            app.toggle_bot_engine()
            app.bot_thread.join(3)
            self.assertEqual(app.core_bot_loop.call_count,2)
            self.assertFalse(app.bot_running)

    def test_failed_worker_clears_running_state(self):
        app=self.app()
        app.core_bot_loop=Mock(side_effect=RuntimeError('test failure'))
        app.bot_running=True
        app._run_bot_worker()
        self.assertFalse(app.bot_running)
        app.root.after.assert_called_once()

if __name__=='__main__':main()
