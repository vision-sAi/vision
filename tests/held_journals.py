"""Lets go of the chat journals a test's Hub claimed, before its temporary folder is deleted.

A claimed journal stays open for as long as the process runs (ChatJournal.claim), and Windows can't
delete an open file, so a test that drops its Hub leaves the folder impossible to clean up there."""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from vision.turnjournal import ChatJournal


@contextmanager
def released_journals():
    """Every journal claimed inside is released on the way out. Put it after the TemporaryDirectory in
    the same `with`, so it runs first."""
    held = []
    claim = ChatJournal.claim

    def claiming(journal):
        ok = claim(journal)
        if ok:
            held.append(journal)
        return ok

    with patch.object(ChatJournal, "claim", claiming):
        try:
            yield
        finally:
            for journal in held:
                journal.release()
