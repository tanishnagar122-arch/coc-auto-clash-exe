"""Regressions for moving wall rows, changing OCR counts, and cancellation."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, main
from unittest.mock import Mock, patch
import cv2
import numpy as np
import bot_core as bot
from verify_builder_sidebar import active_wall_parser

class MovingWalls(TestCase):
    def test_short_strips_recover_labels_missing_from_full_panel_reads(self):
        frame=cv2.imread(str(Path(__file__).parent/'tests'/'fixtures'/'builder-low-gold-scrolled.png'))
        original_ocr=bot.ocr
        calls=0
        def miss_full_panel(*args,**kwargs):
            nonlocal calls
            calls+=1
            rows,timing=original_ocr(*args,**kwargs)
            if calls<=2:
                rows=[item for item in rows or [] if 'wall' not in str(item[1]).lower()]
            return rows,timing
        with patch.object(bot,'ocr',side_effect=miss_full_panel):
            rows=bot.run_layout_independent_builder_ocr(frame,bot.BUILDER_SIDEBAR_CROP)
        options=active_wall_parser()(rows,frame,bot.BUILDER_SIDEBAR_CROP)
        self.assertEqual({o['cost'] for o in options},{15000,25000})

    def test_live_rows_at_different_positions(self):
        parse=active_wall_parser()
        for name in ('builder-low-gold.png','builder-low-gold-scrolled.png'):
            original=cv2.imread(str(Path(__file__).parent/'tests'/'fixtures'/name))
            for scale in (1.0,.8):
                frame=cv2.resize(original,None,fx=scale,fy=scale)
                results=bot.run_layout_independent_builder_ocr(frame,bot.BUILDER_SIDEBAR_CROP)
                options=parse(results,frame,bot.BUILDER_SIDEBAR_CROP)
                self.assertEqual({(o['cost'],o['resource']) for o in options},{(15000,'Elixir'),(25000,'Elixir')},(name,scale,options))

    def test_consensus_keeps_distinct_prices_and_ignores_count_noise(self):
        tree=ast.parse(Path(bot.__file__).read_text(encoding='utf-8'))
        method=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='find_and_upgrade_walls'][-1]
        functions=[n for n in method.body if isinstance(n,ast.FunctionDef) and n.name in ('wall_identity','stabilize_scanned_wall_options','same_wall_target','near_wall_target')]
        ns=dict(vars(bot),sidebar=True,self=SimpleNamespace(write_terminal=Mock()),is_affordable_wall=lambda _:True)
        exec(compile(ast.Module(body=functions,type_ignores=[]),'consensus','exec'),ns)
        rows=[dict(text=f'{label} -> {cost}',cost=cost,resource='Elixir',page_index=page,repeat_confirmed=False)
              for label,cost,page in [('Wall x 11l',15000,0),('Wall x 11',15000,1),('Wall',25000,0),('Wall',25000,1)]]
        confirmed=ns['stabilize_scanned_wall_options'](rows)
        self.assertEqual({r['cost'] for r in confirmed},{15000,25000})
        self.assertTrue(ns['same_wall_target'](rows[0],15000,'Elixir','wall'))
        self.assertFalse(ns['near_wall_target'](rows[0],15500,'Elixir','wall'))

    def test_stop_during_retry_does_not_refind(self):
        logs=[];frame=np.zeros((730,1300,3),dtype=np.uint8)
        app=SimpleNamespace(bot_running=True,capture_screen=lambda _:frame,
            run_ocr_on_region=lambda *_:[(None,'Suggested upgrades')],get_scroll_page_limit=lambda:7,
            write_terminal=logs.append,update_gui_metrics=Mock(),bot_sleep=lambda _:app.bot_running)
        calls=0
        def page(*_):
            nonlocal calls
            calls+=1
            if calls==2:app.bot_running=False
            return []
        mouse=SimpleNamespace(click=Mock(),moveTo=Mock(),scroll=Mock())
        with patch.object(bot,'pyautogui',mouse),patch.object(bot,'ocr',return_value=(None,None)),patch.object(bot,'builder_sidebar_visible',return_value=True),patch.object(bot,'village_builder_anchor',return_value=(.93,.82)),patch.object(bot,'run_layout_independent_builder_ocr',side_effect=page):
            self.assertFalse(bot.FancyCoCBotGUI.find_and_upgrade_walls(app,(0,0,1300,730),23664,1336189))
        mouse.click.assert_not_called()
        mouse.scroll.assert_not_called()
        self.assertFalse(any('reopening' in line or 'Cheapest affordable' in line for line in logs))

if __name__=='__main__':main()
