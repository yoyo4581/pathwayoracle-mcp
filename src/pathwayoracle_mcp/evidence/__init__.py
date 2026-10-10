"""
Evidence ledger: everything the tools returned in an MCP session, as a graph.

The server records (dispatch -> ingest), the model chooses what to show by id
(Phase 2), the server renders only what it recorded (Phase 3). See
docs/ROADMAP.md.
"""
from __future__ import annotations

import threading
from collections import OrderedDict

from .ingest import MAPPERS, ingest
from .ledger import EvidenceLedger

__all__ = ["EvidenceLedger", "Ledgers", "MAPPERS", "ingest"]


class Ledgers:
    """
    One ledger per MCP session. Over stdio a process serves one session; over
    HTTP many, so keep the most recent `max_sessions` and drop the oldest --
    the ledger is working memory for a live conversation, not storage.
    """

    def __init__(self, release: str | None = None, max_sessions: int = 32):
        self.release = release
        self.max_sessions = max_sessions
        self._by_session: OrderedDict[str, EvidenceLedger] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, session: str) -> EvidenceLedger:
        with self._lock:
            lg = self._by_session.get(session)
            if lg is None:
                lg = self._by_session[session] = EvidenceLedger(session, self.release)
                while len(self._by_session) > self.max_sessions:
                    self._by_session.popitem(last=False)
            else:
                self._by_session.move_to_end(session)
            return lg
