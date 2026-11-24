"""Rules, storage and progression. Includes every test from the earlier patches plus regressions
for the bugs fixed in v4 (dedupe memory, care farming, stage mismatch, locking)."""
import sqlite3
import threading

from companion.core import (CARE_DAILY_COIN_CAP, CARE_DAILY_XP_CAP, CompanionCore, GAME_DAILY_COIN_CAP,
                            PetState, stage_for_level, xp_needed)
from companion.store import CompanionStore


def make_core(tmp_path, name="companion.db"):
    return CompanionCore(CompanionStore(tmp_path / name))


def done_job(i, url=None, size=5_000_000):
    return {"id": str(i), "url": url or "https://example.test/video%d.mp4" % i, "status": "done", "size_done": size}


# ---- tests carried over from the earlier patches ---------------------------------------
def test_default_off_and_no_offline_decay(tmp_path):
    c = make_core(tmp_path)
    assert not c.state.enabled
    before = c.state.hunger
    c.tick(c._last_tick + 90000)
    assert c.state.hunger == before


def test_decay_only_while_enabled(tmp_path):
    c = make_core(tmp_path)
    c.set_enabled(True)
    before = c.state.hunger
    c.tick(c._last_tick + 1500)
    assert c.state.hunger == before - 1
    c.set_enabled(False)
    before = c.state.hunger
    c.tick(c._last_tick + 90000)
    assert c.state.hunger == before


def test_completed_download_rewards_once_and_dedupes_url(tmp_path):
    c = make_core(tmp_path)
    c.set_enabled(True)
    job = done_job(1, "https://example.test/video.mp4#one")
    assert c.award_download(job) > 0
    assert c.award_download(job) == 0
    assert c.award_download({**job, "id": "2", "url": "https://example.test/video.mp4#two"}) == 0


def test_failed_and_tiny_jobs_give_nothing(tmp_path):
    c = make_core(tmp_path)
    c.set_enabled(True)
    base = {"id": "1", "url": "https://example.test/a", "size_done": 1000}
    assert c.award_download({**base, "status": "error"}) == 0
    assert c.award_download({**base, "status": "done"}) == 0


def test_care_requires_enabled_and_feed_uses_coin(tmp_path):
    c = make_core(tmp_path)
    assert not c.care("pet")["ok"]
    c.set_enabled(True)
    assert not c.care("feed")["ok"]
    c.state.coins = 1
    assert c.care("feed")["ok"]
    assert c.state.coins == 0


def test_xp_curve_grows():
    assert xp_needed(5) > xp_needed(1)


def test_species_registry_is_data_driven(tmp_path):
    ids = {x["id"] for x in make_core(tmp_path).store.species_registry()}
    assert {"puppy", "cat", "frog", "axolotl"} <= ids


def test_locked_species_cannot_be_selected(tmp_path):
    core = make_core(tmp_path)
    core.set_enabled(True)
    assert not core.set_species("cat")["ok"]
    core.store.unlock_once("species_cat")
    assert core.set_species("cat")["ok"]
    assert core.state.species == "cat"


def test_achievement_unlock_is_idempotent(tmp_path):
    core = make_core(tmp_path)
    core.state.downloads_counted = 1
    core._check_achievements()
    core._check_achievements()
    assert core.store.list_unlocks().count("first_download") == 1
    assert "sticker_first_fetch" in core.store.list_unlocks()


def test_minigame_reward_daily_cap(tmp_path):
    core = make_core(tmp_path)
    core.set_enabled(True)
    a = core.award_minigame(30)
    b = core.award_minigame(30)
    assert a["coins"] + b["coins"] <= GAME_DAILY_COIN_CAP
    assert core.state.games_played == 2


def test_daily_streak_is_idempotent_and_resets_after_gap(tmp_path):
    core = make_core(tmp_path)
    core.set_enabled(True)
    assert core.mark_daily_open("2026-11-20") == 1
    assert core.mark_daily_open("2026-11-20") == 1
    assert core.mark_daily_open("2026-11-21") == 2
    assert core.mark_daily_open("2026-11-24") == 1


def test_v1_schema_migrates_additively(tmp_path):
    path = tmp_path / "legacy.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE companion_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    c.execute("CREATE TABLE companion_state (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL, updated_ts REAL NOT NULL)")
    c.execute("CREATE TABLE companion_inventory (item_id TEXT PRIMARY KEY, quantity INTEGER NOT NULL DEFAULT 0)")
    c.execute("CREATE TABLE companion_unlocks (unlock_id TEXT PRIMARY KEY, unlocked_ts REAL NOT NULL)")
    c.execute("CREATE TABLE stickers (sticker_id TEXT PRIMARY KEY, filename TEXT NOT NULL, manifest TEXT NOT NULL, created_ts REAL NOT NULL)")
    c.execute("CREATE TABLE companion_events (seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT NOT NULL, payload TEXT NOT NULL, created_ts REAL NOT NULL)")
    c.execute("INSERT INTO companion_meta VALUES('schema_version','1')")
    c.execute("INSERT INTO companion_state VALUES(1,'{\"enabled\":true,\"species\":\"puppy\"}',0)")
    c.commit()
    c.close()
    store = CompanionStore(path)
    assert store.load_state()["species"] == "puppy"
    assert store.daily_game_rewards("2026-11-22") == 0


# ---- v4 regressions -------------------------------------------------------------------------
def test_stage_follows_level_from_the_start(tmp_path):
    assert PetState().stage == "egg"
    assert PetState.from_dict({"level": 9, "stage": "baby"}).stage == "kid"
    assert [stage_for_level(n) for n in (1, 2, 3, 7, 8, 15, 16)] == ["egg", "egg", "baby", "baby", "kid", "kid", "adult"]


def test_hatching_emits_evolved_and_unlocks_achievement(tmp_path):
    core = make_core(tmp_path)
    core.set_enabled(True)
    seen = []
    core.add_listener(lambda kind, data: seen.append((kind, data)))
    core.state.level, core.state.xp = 2, xp_needed(2) - 1
    core.award_download(done_job(1))
    kinds = [k for k, _ in seen]
    assert "evolved" in kinds and "level_up" in kinds
    assert core.state.stage == "baby"
    assert core.is_unlocked("hatched")
    assert core.reaction() == "celebrating"


def test_download_dedupe_survives_event_log_churn(tmp_path):
    core = make_core(tmp_path)
    core.set_enabled(True)
    job = done_job(1, "https://example.test/keep.mp4")
    assert core.award_download(job) > 0
    for i in range(700):                       # far past the 500 row event log
        core.store.add_event("care", {"i": i})
    again = {**job, "id": "99"}
    assert core.award_download(again) == 0     # v2 forgot the URL here and paid twice


def test_care_loop_cannot_be_farmed(tmp_path):
    core = make_core(tmp_path)
    core.set_enabled(True)
    start_coins = core.state.coins
    for _ in range(300):
        core._cooldowns = {}                   # ignore cooldowns: test the daily ceilings
        core.care("nap")
        core.care("play")
    assert core.state.coins - start_coins <= CARE_DAILY_COIN_CAP
    assert core.state.daily_care_xp <= CARE_DAILY_XP_CAP


def test_care_cooldown_and_full_checks(tmp_path):
    core = make_core(tmp_path)
    core.set_enabled(True)
    assert core.care("pet")["ok"]
    assert not core.care("pet")["ok"]          # cooldown
    core.state.energy = 90
    core._cooldowns = {}
    assert not core.care("nap")["ok"]          # not sleepy
    core.state.hunger, core.state.coins = 99, 5
    assert not core.care("feed")["ok"]         # full: no coin is wasted
    assert core.state.coins == 5


def test_minigames_per_game_rules_and_best_scores(tmp_path):
    core = make_core(tmp_path)
    core.set_enabled(True)
    r = core.award_minigame(9, "snake")
    assert r["ok"] and r["coins"] == 3 and r["new_best"]
    assert not core.award_minigame(3, "snake")["new_best"]
    assert core.state.best_scores["snake"] == 9
    assert not core.award_minigame(5, "nope")["ok"]
    core.award_minigame(1, "catch_bytes")
    core.award_minigame(1, "copy_me")
    assert core.is_unlocked("all_games")
    assert core.minigame_remaining() <= GAME_DAILY_COIN_CAP


def test_listener_runs_outside_the_lock_and_can_call_back(tmp_path):
    core = make_core(tmp_path)
    core.set_enabled(True)
    results = []

    def listener(kind, data):
        results.append(core.snapshot()["level"])     # would deadlock if called under a non-reentrant lock

    core.add_listener(listener)
    core.care("pet")
    assert results


def test_concurrent_awards_stay_consistent(tmp_path):
    core = make_core(tmp_path)
    core.set_enabled(True)
    core.state.daily_xp = -10_000                      # keep the daily cap out of this test
    core.state.last_daily = core._today()
    awarded = []

    def work(start):
        for i in range(start, start + 10):
            if core.award_download(done_job(i)) > 0:
                awarded.append(i)

    threads = [threading.Thread(target=work, args=(n * 100,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(awarded) == 40
    assert core.state.downloads_counted == 40


def test_legacy_data_in_the_main_db_is_copied_once(tmp_path):
    main = tmp_path / "main.db"
    legacy = CompanionStore(main)                      # stands in for the v2 tables inside the main DB
    legacy.save_state({"enabled": True, "level": 6, "species": "puppy", "coins": 4})
    legacy.unlock_once("species_cat")
    new = CompanionStore(tmp_path / "companion.db")
    assert new.import_legacy(main) is True
    assert new.load_state()["level"] == 6
    assert "species_cat" in new.list_unlocks()
    assert new.import_legacy(main) is False            # idempotent


def test_snapshot_is_light_by_default_and_full_on_request(tmp_path):
    core = make_core(tmp_path)
    light, full = core.snapshot(), core.snapshot(full=True)
    assert "species_catalog" not in light and "species_catalog" in full
    assert light["pose"] and light["speech"] and light["xp_needed"] > 0


def test_reset_and_options(tmp_path):
    core = make_core(tmp_path)
    core.set_enabled(True)
    core.state.level = 5
    assert core.set_option("bubbles", False)["ok"] and not core.state.bubbles
    assert not core.set_option("nonsense", True)["ok"]
    core.reset()
    assert core.state.level == 1 and not core.state.enabled and core.store.list_unlocks() == []


def test_egg_cracks_only_close_to_hatching(tmp_path):
    core = make_core(tmp_path)
    core.state.level, core.state.xp = 1, 0
    assert core.snapshot()["pose"] == "idle"
    core.state.level, core.state.xp = 2, int(xp_needed(2) * 0.6)
    assert core.snapshot()["pose"] == "cracked"
    core.state.level, core.state.xp = 3, 0
    assert core.snapshot()["stage"] in ("baby", "egg")      # stage is refreshed on the next save
