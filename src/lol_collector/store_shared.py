from __future__ import annotations

import sqlite3
from threading import RLock


class StoreContext:
    def __init__(self, connection: sqlite3.Connection, lock: RLock) -> None:
        self.connection = connection
        self.lock = lock
