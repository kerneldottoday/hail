import os
import stat
import sys

import pytest

from hail.store import Store, default_store

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions only")


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_new_journal_is_private(tmp_path):
    db = tmp_path / "home" / "hail.db"
    Store(db).add("t-1", 1, "user", "hermes", "ask", "advice", "hello")
    assert mode(db.parent) == 0o700
    assert mode(db) == 0o600
    for extra in db.parent.glob("hail.db-*"):  # WAL and SHM files follow the main file
        assert mode(extra) & 0o077 == 0


def test_existing_journal_is_tightened(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o775)
    os.chmod(home, 0o775)
    db = home / "hail.db"
    db.touch()
    os.chmod(db, 0o644)
    Store(db)
    assert mode(home) == 0o700
    assert mode(db) == 0o600


def test_default_store_is_private():
    store = default_store()
    assert mode(store.path) == 0o600
    assert mode(store.path.parent) == 0o700
