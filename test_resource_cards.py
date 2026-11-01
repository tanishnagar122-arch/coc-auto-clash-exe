"""Resource cards must have an Upgrade label, matching price, and adjacent icon."""
from pathlib import Path
from unittest import TestCase,main
import cv2
import bot_core as bot

class ResourceCards(TestCase):
    def test_coin_merged_into_price(self):
        source=cv2.imread(str(Path(__file__).parent/'tests'/'fixtures'/'selected-wall-button-failure.png'))
        for scale in (1,.8):
            frame=cv2.resize(source,None,fx=scale,fy=scale)
            read=lambda image,box:bot.FancyCoCBotGUI.run_ocr_on_region(None,image,box)
            point=bot.wall_resource_button(frame,read,'Gold',25000)
            self.assertIsNotNone(point)
            self.assertLess(abs(point['x']/scale-785),4)
            self.assertLess(abs(point['y']/scale-622),4)
            self.assertIsNone(bot.wall_resource_button(frame,read,'Gold',15000))

    def test_shifted_cards_and_rejection(self):
        source=cv2.imread(str(Path(__file__).parent/'tests'/'fixtures'/'selected-wall-shifted.png'))
        for scale in (1,.8):
            frame=cv2.resize(source,None,fx=scale,fy=scale)
            cache={}
            def read(image,box):
                if box not in cache: cache[box]=bot.FancyCoCBotGUI.run_ocr_on_region(None,image,box)
                return cache[box]
            for resource,expected_x in [('Gold',783),('Elixir',884)]:
                point=bot.wall_resource_button(frame,read,resource,15000)
                self.assertIsNotNone(point)
                self.assertLess(abs(point['x']/scale-expected_x),4)
                self.assertLess(abs(point['y']/scale-621),4)
                self.assertIsNone(bot.wall_resource_button(frame,read,resource,25000))
            def no_upgrade(image,box):
                return [item for item in read(image,box) if 'upgrade' not in item[1].lower()]
            self.assertIsNone(bot.wall_resource_button(frame,no_upgrade,'Elixir',15000))

if __name__=='__main__': main()
