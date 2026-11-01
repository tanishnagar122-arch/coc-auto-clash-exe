"""Repeated identical pages reuse reads; changed pixels must invalidate them."""
from unittest import TestCase,main
from unittest.mock import Mock,patch
import numpy as np
import bot_core as bot

class BuilderReadCache(TestCase):
    def test_exact_image_reuse_and_invalidation(self):
        frame=np.zeros((730,1300,3),dtype=np.uint8)
        def engine(*args,**kwargs):
            return [ [[[10,10],[50,10],[50,30],[10,30]],'Cannon',.99] ],None
        reader=Mock(side_effect=engine)
        bot.BUILDER_OCR_CACHE.clear()
        with patch.object(bot,'ocr',reader):
            first=bot.run_layout_independent_builder_ocr(frame,bot.BUILDER_SIDEBAR_CROP)
            first_calls=reader.call_count
            self.assertGreater(first_calls,0)
            first[0][1]='Modified by caller'
            second=bot.run_layout_independent_builder_ocr(frame,bot.BUILDER_SIDEBAR_CROP)
            self.assertEqual(reader.call_count,first_calls)
            self.assertEqual(second[0][1],'Cannon')
            # Changes outside the list cannot move a wall card.
            frame[10,10]=255
            bot.run_layout_independent_builder_ocr(frame,bot.BUILDER_SIDEBAR_CROP)
            self.assertEqual(reader.call_count,first_calls)
            frame[200,1100]=255
            bot.run_layout_independent_builder_ocr(frame,bot.BUILDER_SIDEBAR_CROP)
            self.assertGreater(reader.call_count,first_calls)

if __name__=='__main__':main()
