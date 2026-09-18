import settings


def test_safe_filename_strips_illegal():
    assert settings.safe_filename('a<b>c:d"e/f\\g|h?i*j') == "abcdefghij"


def test_category_for_known_extensions():
    assert settings.category_for("movie.mp4") == "Video"
    assert settings.category_for("song.mp3") == "Audio"
    assert settings.category_for("unknown.xyz99") in settings.CATEGORIES
