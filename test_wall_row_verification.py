"""An OCR row match must keep its price and position across two captures."""
import ast
import copy
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase,main
from unittest.mock import Mock
import bot_core as bot

class RowVerification(TestCase):
    def test_moved_row_discards_previous_coordinates(self):
        tree=ast.parse(Path(bot.__file__).read_text(encoding='utf-8'))
        method=[n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='find_and_upgrade_walls'][-1]
        refresh=next(n for n in method.body if isinstance(n,ast.FunctionDef) and n.name=='refresh_current_page_match')
        def option(y):
            return dict(cost=15000,resource='Elixir',text='Wall x 7',wall_bbox=[[100,y],[240,y],[240,y+30],[100,y+30]],cost_bbox=[[490,y+32],[610,y+32],[610,y+64],[490,y+64]])
        old,new=option(400),option(510)
        reads=iter([[old],[new],[new]])
        read=Mock(side_effect=lambda *_:next(reads))
        app=SimpleNamespace(bot_running=True,write_terminal=Mock())
        ns=dict(vars(bot),self=app,w=1300,sidebar=True,menu_crop=bot.BUILDER_SIDEBAR_CROP,
            settled_builder_frame=lambda:object(),run_layout_independent_builder_ocr=read,
            parse_wall_options=lambda rows,*_:copy.deepcopy(rows),wall_identity=lambda _:'wall',
            is_affordable_wall=lambda _:True,same_wall_target=lambda row,cost,*_:row['cost']==cost)
        exec(compile(ast.Module(body=[refresh],type_ignores=[]),'row_verification','exec'),ns)
        point,_=ns['refresh_current_page_match'](15000,'Elixir','wall')
        self.assertEqual(point['wall_bbox'],new['wall_bbox'])
        self.assertEqual(read.call_count,3)
        self.assertTrue(any('discarding old coordinates' in str(call) for call in app.write_terminal.call_args_list))

if __name__=='__main__':main()
