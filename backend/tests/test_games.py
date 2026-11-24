"""Mini-game rules. Qt is replaced by a stub when PySide6 is not installed, because the rules
(update/press) never paint. Painting is covered by running the app."""
import random
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


from qt_stub import install as _install_qt_stub  # noqa: E402

_install_qt_stub()

from companion.core import GAMES  # noqa: E402
from companion.games import GAME_CLASSES  # noqa: E402
from companion.games.catch_bytes import LANES, PLAYER_TOP, CatchBytes  # noqa: E402
from companion.games.copy_me import MAX_ROUNDS, CopyMe  # noqa: E402
from companion.games.snake import COLS, TreatTrail  # noqa: E402


def ctx(seed=1):
    return types.SimpleNamespace(rng=random.Random(seed), pet=lambda pose: None)


def run(game, seconds, step=0.02):
    for _ in range(int(seconds / step)):
        game.update(step)
        if game.done:
            break


def test_every_game_id_matches_a_core_reward_rule():
    assert {cls.id for cls in GAME_CLASSES} == set(GAMES)


def test_catch_lane_movement_clamps():
    g = CatchBytes(ctx())
    for _ in range(10):
        g.press("left")
    assert g.lane == 0
    for _ in range(10):
        g.press("right")
    assert g.lane == LANES - 1


def test_catch_scores_bytes_and_stars_and_bugs_cost_hearts():
    g = CatchBytes(ctx())
    g.items = [{"lane": g.lane, "y": PLAYER_TOP, "kind": "byte", "speed": 0}]
    g.update(0.01)
    assert g.score == 1 and not g.items
    g.items = [{"lane": g.lane, "y": PLAYER_TOP, "kind": "star", "speed": 0}]
    g.update(0.01)
    assert g.score == 4
    g.items = [{"lane": g.lane, "y": PLAYER_TOP, "kind": "bug", "speed": 0}]
    g.update(0.01)
    assert g.lives == 2 and g.score == 4


def test_catch_ignores_items_in_other_lanes_and_ends_on_time_or_hearts():
    g = CatchBytes(ctx())
    other = (g.lane + 1) % LANES
    g.items = [{"lane": other, "y": PLAYER_TOP, "kind": "bug", "speed": 0}]
    g.update(0.01)
    assert g.lives == 3
    run(g, 40)
    assert g.done and g.time_left == 0
    h = CatchBytes(ctx())
    h.lives = 1
    h.items = [{"lane": h.lane, "y": PLAYER_TOP, "kind": "bug", "speed": 0}]
    h.update(0.01)
    assert h.done


def test_catch_pause_freezes_everything():
    g = CatchBytes(ctx())
    g.paused = True
    before = (g.time_left, list(g.items), g.lane)
    g.press("left")
    g.update(1.0)
    assert (g.time_left, g.items, g.lane) == before


def test_snake_ignores_reversal_and_queues_turns():
    g = TreatTrail(ctx())
    g.press("left")                       # opposite of the starting direction
    assert g.queue == []
    g.press("up")
    g.press("up")                         # a repeat of the last queued turn is ignored
    assert g.queue == ["up"]


def test_snake_eats_grows_and_speeds_up():
    g = TreatTrail(ctx())
    head = g.body[0]
    g.food = (head[0] + 1, head[1])
    length = len(g.body)
    g.update(0.2)
    assert g.score == 1 and len(g.body) == length + 1 and g.food != (head[0] + 1, head[1])
    assert g._interval() < 0.18


def test_snake_dies_on_wall_and_on_itself():
    g = TreatTrail(ctx())
    g.body = [(COLS - 1, 5), (COLS - 2, 5), (COLS - 3, 5)]
    g.dir = "right"
    g.update(0.25)
    assert g.done
    s = TreatTrail(ctx())
    s.body = [(5, 5), (5, 6), (6, 6), (6, 5), (6, 4), (5, 4)]
    s.dir = "right"
    s.food = (0, 0)
    s.update(0.25)
    assert s.done


def test_copy_me_full_round_and_progression():
    g = CopyMe(ctx(3))
    for _ in range(400):
        g.update(0.05)
        if g.state == "input":
            break
    assert g.state == "input" and len(g.seq) == 1
    g.press(g.seq[0])
    assert g.state == "good" and g.score == 1
    for _ in range(100):
        g.update(0.05)
        if g.state == "input":
            break
    assert g.round == 2 and len(g.seq) == 2


def test_copy_me_wrong_press_ends_the_game_after_a_beat():
    g = CopyMe(ctx(4))
    for _ in range(400):
        g.update(0.05)
        if g.state == "input":
            break
    wrong = next(d for d in ("up", "down", "left", "right") if d != g.seq[0])
    g.press(wrong)
    assert g.state == "wrong" and not g.done
    run(g, 2.0)
    assert g.done and g.score == 0


def test_copy_me_perfect_run_ends_at_max_rounds():
    g = CopyMe(ctx(5))
    for _ in range(40000):
        g.update(0.05)
        if g.state == "input":
            for step in list(g.seq):
                g.press(step)
        if g.done:
            break
    assert g.done and g.score == MAX_ROUNDS
