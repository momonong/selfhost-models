"""Run the real Windows CLI entry while forbidding any SQLite connection."""
import sqlite3
import sys

def forbidden(*args, **kwargs):
    raise AssertionError("Windows client attempted SQLite access")

sqlite3.connect = forbidden
from selfhost_models.cli import main
sys.exit(main())
