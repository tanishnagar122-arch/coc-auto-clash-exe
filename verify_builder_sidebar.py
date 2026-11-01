"""Regression checks: python verify_builder_sidebar.py <open-panel screenshot>."""
import ast
import copy
from pathlib import Path
import re
import sys

import cv2
import bot_core as bot


def active_wall_parser():
    tree = ast.parse(Path(bot.__file__).read_text(encoding="utf-8"))
    methods = [node for cls in tree.body if isinstance(cls, ast.ClassDef)
               for node in cls.body if isinstance(node, ast.FunctionDef)
               and node.name == "find_and_upgrade_walls"]
    parser = next(node for node in methods[-1].body
                  if isinstance(node, ast.FunctionDef) and node.name == "parse_wall_options")
    namespace = dict(vars(bot))
    exec(compile(ast.Module(body=[parser], type_ignores=[]), "wall_parser", "exec"), namespace)
    return namespace["parse_wall_options"]


def check(screenshot):
    # Window bounds in the supplied 1920x1080 reference image.
    source = cv2.imread(str(screenshot))
    assert source is not None, "Cannot read reference screenshot"
    frame = source[151:880, 277:1577]
    read = lambda image, box: bot.FancyCoCBotGUI.run_ocr_on_region(None, image, box)
    parse = active_wall_parser()
    for scale in (1.0, 0.8):
        image = cv2.resize(frame, None, fx=scale, fy=scale)
        assert bot.builder_sidebar_visible(image, read)
        assert bot.village_home_layout(image, read) is None
        results = bot.run_layout_independent_builder_ocr(image, bot.BUILDER_SIDEBAR_CROP)
        assert parse(results, image, bot.BUILDER_SIDEBAR_CROP) == [], "Non-wall selected"
        prices = {re.sub(r"\D", "", item[1]): item for item in results
                  if re.fullmatch(r"[\d ]+", item[1])}
        for price, resource in (("1000", "Gold"), ("1000000", "Elixir")):
            item = prices[price]
            assert bot.detect_builder_cost_resource(
                image, item[0], results, bot.BUILDER_SIDEBAR_CROP) == resource
        # Reuse actual card geometry, replacing only OCR labels. This exercises
        # label/price vertical offsets and adjacent rows, not synthetic pixels.
        wall_results = copy.deepcopy(results)
        for item in wall_results:
            if "warden" in item[1].lower():
                item[1] = "Wall x123"
        options = parse(wall_results, image, bot.BUILDER_SIDEBAR_CROP)
        assert len(options) == 1, options
        assert options[0]["cost"] == 1000000, options
        assert options[0]["resource"] == "Elixir", options
        bbox = options[0]["wall_bbox"]
        click_x = image.shape[1] * 0.75 + (bbox[0][0] + bbox[2][0]) / 4
        click_y = image.shape[0] * 0.12 + (bbox[0][1] + bbox[2][1]) / 4
        assert 0.78 < click_x / image.shape[1] < 0.90
        assert 0.32 < click_y / image.shape[0] < 0.39
        blank = image.copy()
        blank[:] = 0
        assert not bot.builder_sidebar_visible(blank, read)
        print(f"PASS {scale:g}x: panel, prices, wall matching, click coordinates, negative checks")


def check_live_fixtures():
    fixtures = Path(__file__).parent / "tests" / "fixtures"
    read = lambda image, box: bot.FancyCoCBotGUI.run_ocr_on_region(None, image, box)
    parse = active_wall_parser()
    for scale in (1.0, 0.8):
        image = cv2.resize(cv2.imread(str(fixtures / "builder-walls.png")), None, fx=scale, fy=scale)
        results = bot.run_layout_independent_builder_ocr(image, bot.BUILDER_SIDEBAR_CROP)
        options = parse(results, image, bot.BUILDER_SIDEBAR_CROP)
        assert {item["cost"] for item in options} == {15000, 25000}, options
        assert all(item["resource"] == "Gold" for item in options)
        snapshot = bot.builder_list_snapshot(image, bot.BUILDER_SIDEBAR_CROP)
        assert bot.builder_list_unchanged(snapshot, snapshot.copy())
        shifted = bot.np.roll(snapshot, 12, axis=0)
        assert not bot.builder_list_unchanged(snapshot, shifted)
        confirm = cv2.resize(cv2.imread(str(fixtures / "wall-confirmation.png")), None, fx=scale, fy=scale)
        assert bot.wall_confirmation_button(confirm, read, 15000, 6)
        assert bot.wall_confirmation_button(confirm, read, 30000, 6) is None
        assert bot.wall_confirmation_button(confirm, read, 15000, 7) is None
        selected = cv2.resize(cv2.imread(str(fixtures / "selected-wall.png")), None, fx=scale, fy=scale)
        assert bot.wall_confirmation_button(selected, read, 15000, 6) is None
        print(f"PASS {scale:g}x: real wall rows, scroll movement, correct price/level, wrong-dialog rejection")
    after = cv2.imread(str(fixtures / "upgraded-wall.png"))
    title = " ".join(item[1] for item in read(after, (0.64, 0.80, 0.34, 0.66)))
    assert re.search(r"wall.*level\s*6", title, re.I), title
    print("PASS: completed wall upgrade detected as level 6")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        check(Path(sys.argv[1]))
    check_live_fixtures()
