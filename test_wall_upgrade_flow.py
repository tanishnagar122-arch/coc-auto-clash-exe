"""Real screenshot regressions; all desktop input is mocked."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import copy
import cv2
import bot_core as bot
from verify_builder_sidebar import active_wall_parser

fixtures = Path(__file__).parent / 'tests' / 'fixtures'
read = lambda im, box: bot.FancyCoCBotGUI.run_ocr_on_region(None, im, box)
parse = active_wall_parser()
tree = ast.parse(Path(bot.__file__).read_text(encoding='utf-8'))
click_function = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'click_wall_option'][-1]

for scale in (1.0, .8):
    im = cv2.resize(cv2.imread(str(fixtures / 'builder-walls-missed-label.png')), None, fx=scale, fy=scale)
    results = bot.run_layout_independent_builder_ocr(im, bot.BUILDER_SIDEBAR_CROP)
    options = parse(results, im, bot.BUILDER_SIDEBAR_CROP)
    assert {o['cost'] for o in options} == {15000, 25000}, options
    # Vary count and price independently, preserving actual neighboring rows.
    for count, cost in ((1,50), (2,125), (137,999), (999,1250000), (2500,12345678)):
        varied = copy.deepcopy(results)
        target = next(o for o in options if o['cost'] == 25000)
        for item in varied:
            if item[0] == target['wall_bbox']: item[1] = f'Wall x {count}'
            if item[0] == target['cost_bbox']: item[1] = f'{cost:,}'.replace(',', ':')
        parsed = parse(varied, im, bot.BUILDER_SIDEBAR_CROP)
        assert any(o['cost'] == cost and f'x {count}' in o['text'] for o in parsed), parsed
    for suffix, cost in (('',15000), ('-current',25000)):
        frames = [cv2.resize(cv2.imread(str(fixtures / f'{name}{suffix}.png')),None,fx=scale,fy=scale)
                  for name in ('selected-wall','wall-confirmation','upgraded-wall')]
        h,w=frames[0].shape[:2]
        captures=iter([frames[0], frames[0], frames[0], frames[1], frames[2]])
        logs=[]
        app=SimpleNamespace(bot_running=True,capture_screen=lambda rect:next(captures),run_ocr_on_region=read,
            write_terminal=logs.append,bot_sleep=lambda _:True,update_gui_metrics=Mock(),press_real_escape=Mock())
        mouse=SimpleNamespace(click=Mock(),moveTo=Mock())
        namespace=dict(vars(bot),self=app,x=0,y=0,w=w,h=h,win_rect=(0,0,w,h),sidebar=True,
            close_builder_menu=lambda:True,current_gold=2000000,current_elixir=1000000,pyautogui=mouse)
        exec(compile(ast.Module(body=[click_function],type_ignores=[]),'active_wall_click','exec'),namespace)
        bbox=[[20,20],[180,20],[180,50],[20,50]]
        target={'wall_bbox':bbox,'resource':'Gold','cost':cost}
        assert namespace['click_wall_option'](target,[(bbox,'Wall x 38')],bot.BUILDER_SIDEBAR_CROP), logs
        assert mouse.click.call_count==3, mouse.click.call_args_list
        resource_click=mouse.click.call_args_list[1].args
        assert abs(resource_click[0]/scale-735)<6 and abs(resource_click[1]/scale-622)<6,resource_click
        assert app._wall_expected_wallet==(2000000-cost,1000000)
        assert not app.press_real_escape.called
        print(f'PASS {scale}x: {cost} wall selection -> resource -> confirmation -> verified level')
    print(f'PASS {scale}x: missed label recovered; varied wall counts and prices')
assert bot.normalize_wall_level_text('Wall (Level S)') == 'Wall (Level 5)'
assert bot.normalize_wall_level_text('Cannon (Level S)') == 'Cannon (Level S)'
print('PASS: level OCR correction restricted to walls')
