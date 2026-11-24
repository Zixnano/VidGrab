"""Mini-games played on the handheld shell. Qt is imported only by the game modules."""
from .catch_bytes import CatchBytes
from .copy_me import CopyMe
from .snake import TreatTrail

GAME_CLASSES = (CatchBytes, TreatTrail, CopyMe)


def by_id(game_id):
    for cls in GAME_CLASSES:
        if cls.id == game_id:
            return cls
    return None
