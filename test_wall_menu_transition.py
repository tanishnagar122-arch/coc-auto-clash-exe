"""Use the supplied open/closed screenshots to reject stale Upgrade positions."""
from pathlib import Path
from unittest import TestCase,main
from unittest.mock import Mock
import cv2
import bot_core as bot

class MenuTransition(TestCase):
    def test_discards_open_panel_position_for_both_resources(self):
        folder=Path(__file__).parent/'tests'/'fixtures'
        for resource,expected_x in [('Gold',783),('Elixir',884)]:
            for scale in (1,.8):
                opened=cv2.resize(cv2.imread(str(folder/'wall-menu-transition-042229.png')),None,fx=scale,fy=scale)
                closed=cv2.resize(cv2.imread(str(folder/'wall-menu-transition-042241.png')),None,fx=scale,fy=scale)
                frames=iter([opened,closed,closed])
                cache={}
                def read(image,box):
                    key=(id(image),box)
                    if key not in cache:cache[key]=bot.FancyCoCBotGUI.run_ocr_on_region(None,image,box)
                    return cache[key]
                report=Mock()
                button=bot.settled_wall_resource_button(lambda:next(frames),read,resource,15000,lambda _:True,lambda:True,report)
                self.assertIsNotNone(button,(resource,scale))
                self.assertLess(abs(button['x']/scale-expected_x),5)
                self.assertLess(abs(button['y']/scale-621),5)
                self.assertTrue(any('still visible' in str(call) for call in report.call_args_list))
                print('PASS',resource,scale,'discarded open-menu position; confirmed closed-menu button',flush=True)

if __name__=='__main__':main()
