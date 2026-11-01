"""Run the production wall routine with recorded screens and mocked input."""
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock,patch
import cv2
import bot_core as bot
from verify_builder_sidebar import active_wall_parser

class FullWallDrain(TestCase):
    def test_cheaper_wall_recovered_during_verify_or_refind(self):
        folder=Path(__file__).parent/'tests'/'fixtures'
        frames={name:cv2.imread(str(folder/file)) for name,file in {
            'list':'wall-row-mismatch-live.png','selected':'wall-row-selected-five-live.png',
            'confirm':'wall-row-confirm-clear-live.png','after':'wall-row-upgraded-six-live.png'}.items()}
        all_rows=bot.run_layout_independent_builder_ocr(frames['list'],bot.BUILDER_SIDEBAR_CROP)
        real_ocr=bot.ocr
        options=active_wall_parser()(all_rows,frames['list'],bot.BUILDER_SIDEBAR_CROP)
        cheap=next(o for o in options if o['cost']==15000)
        expensive_only=[i for i in all_rows if i[0] not in (cheap['wall_bbox'],cheap['cost_bbox'])]
        for recover_during in ('verify','refind','backtrack'):
            with self.subTest(recover_during=recover_during):
                state={'stage':'list','open':True,'reads':0,'page':0,'wheels':0};logs=[];clicks=[];cache={}
                def read_page(*_):
                    state['reads']+=1
                    if recover_during=='backtrack':
                        return copy.deepcopy(all_rows if state['page']==1 else [])
                    cutoff=1 if recover_during=='verify' else 2
                    return copy.deepcopy(expensive_only if state['reads']<=cutoff else all_rows)
                def read_region(frame,box):
                    if box==(.115,.22,.75,1.0):return [(None,'Suggested upgrades')]
                    key=(state['stage'],box)
                    if key not in cache:
                        with patch.object(bot,'ocr',real_ocr):
                            cache[key]=bot.FancyCoCBotGUI.run_ocr_on_region(None,frame,box)
                    return cache[key]
                def click(x,y):
                    clicks.append((x,y))
                    if x>1250 and y<100:state['open']=False
                    elif x>1180 and 560<y<630:state['open']=True
                    elif state['stage']=='list' and state['open']:
                        expected=frames['list'].shape[0]*.12+sum(p[1] for p in cheap['cost_bbox'])/8
                        self.assertLess(abs(y-expected),4,'Must select cheaper row')
                        state['stage']='selected'
                    elif state['stage']=='selected':
                        self.assertLess(abs(x-885),6,'Must click Elixir Upgrade')
                        self.assertLess(abs(y-622),6)
                        state['stage']='confirm'
                    elif state['stage']=='confirm':state['stage']='after'
                app=SimpleNamespace(bot_running=True,capture_screen=lambda _:frames[state['stage']],
                    run_ocr_on_region=read_region,get_scroll_page_limit=lambda:3 if recover_during=='backtrack' else 1,write_terminal=logs.append,
                    update_gui_metrics=Mock(),bot_sleep=lambda _:True,press_real_escape=Mock())
                def wheel(delta,**kwargs):
                    state['wheels']+=1
                    if state['wheels']%4==0:state['page']+=1 if delta<0 else -1
                mouse=SimpleNamespace(click=click,moveTo=Mock(),scroll=Mock(side_effect=wheel))
                with patch.object(bot,'pyautogui',mouse),patch.object(bot,'ocr',return_value=(None,None)),patch.object(bot,'builder_sidebar_visible',side_effect=lambda *_:state['open']),patch.object(bot,'village_builder_anchor',return_value=(.93,.82)),patch.object(bot,'run_layout_independent_builder_ocr',side_effect=read_page),patch.object(bot.time,'sleep'):
                    self.assertTrue(bot.FancyCoCBotGUI.find_and_upgrade_walls(app,(0,0,1300,730),58036,1356711),logs)
                self.assertEqual(state['stage'],'after',logs)
                self.assertEqual(app._wall_expected_wallet,(58036,1341711))
                self.assertTrue(any('Wall upgrade confirmed' in line for line in logs),logs)
                self.assertFalse(app.press_real_escape.called)
                self.assertEqual(sum(x>1250 and y<100 for x,y in clicks),1,'Close panel only after selecting wall')
                self.assertFalse(any('Closing and reopening' in line for line in logs),logs)
                if recover_during=='backtrack':
                    self.assertEqual(mouse.scroll.call_count,12)
                else:mouse.scroll.assert_not_called()
                print('PASS full production routine:',recover_during,'recovered 15,000 wall; selected Elixir; confirmed and verified upgrade',flush=True)

if __name__=='__main__':main()
