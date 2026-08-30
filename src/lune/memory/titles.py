"""Generation-fenced orchestration for a thread's one automatic title.

The store already refuses to replace a renamed or already-generated title
(:meth:`MemoryStore.set_generated_conversation_title`).  This module decides
*when* that single attempt happens and guarantees the attempt can never cost the
conversation anything: a backend that fails, returns nothing, or is overtaken by
a cancelled generation simply leaves the default title in place.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final, Protocol

from lune.llm.contracts import LOCAL_MODEL_NAME, ModelName
from lune.memory.store import MemoryStore, StoredTurn

MAX_TITLE_CHARACTERS: Final[int] = 24
"""The store accepts 160; the sidebar shows one line, so ask for far less."""

# Model output likes to wrap a title in brackets or end it with a full stop.
# The CJK punctuation is written as escapes because `RUF001` reads a fullwidth
# character in a literal as a confusable typo, which it only is in prose.
_STRIPPED_EDGES: Final[str] = (
    " \t\"'()[]<>-_.,:!?"
    "\u2018\u2019\u201c\u201d\u2013\u2014\u00b7"
    "\u300c\u300d\u300e\u300f\u300a\u300b\u3008\u3009\u3010\u3011"
    "\uff08\uff09\u3002\uff0c\u3001\uff1a\uff01\uff1f"
)


@dataclass(frozen=True, slots=True)
class ThreadTitleRequest:
    """One completed turn, offered to whichever model already holds the context."""

    generation_id: int
    turn: StoredTurn = field(repr=False)
    max_characters: int = MAX_TITLE_CHARACTERS
    # The test phase names threads with the on-device model. The hybrid form
    # names them with the fallback model that is already loaded for the turn;
    # neither is allowed to open a cloud request of its own for a title.
    model: ModelName = LOCAL_MODEL_NAME


class ThreadTitleBackend(Protocol):
    """Return a candidate title, or an empty string for "no title this time".

    Returning empty is how a backend reports a cancelled or unusable generation:
    it is not an error, and it must leave the default title untouched.
    """

    async def __call__(self, request: ThreadTitleRequest) -> str: ...


class ThreadTitleManager:
    """Name a thread once, after its first turn actually completes."""

    def __init__(
        self,
        store: MemoryStore,
        backend: ThreadTitleBackend,
        *,
        max_characters: int = MAX_TITLE_CHARACTERS,
    ) -> None:
        if not 1 <= max_characters <= 160:
            raise ValueError("a generated title must fit the store's 160-character bound")
        self._store = store
        self._backend = backend
        self._max_characters = max_characters

    async def maybe_title(
        self,
        session_id: str,
        *,
        generation_id: int,
        is_generation_current: Callable[[int], bool],
    ) -> str | None:
        """Generate the automatic title if this thread is owed exactly one.

        The fence is checked twice: before spending anything on a backend, and
        again before the write, because the user can barge in while the model is
        still choosing words.  A cancelled generation leaves no title behind, in
        the same way it leaves no transcript behind.
        """

        if generation_id < 0:
            raise ValueError("generation ID cannot be negative")
        thread = self._store.get_conversation_thread(session_id)
        if thread is None or thread.title_source != "default":
            return None
        # Exactly one completed turn is what "after the first round" means. It
        # also keeps a failed attempt from re-running on every later turn: the
        # spec asks for one automatic title, not one attempt per turn.
        turns = self._store.recent_complete_turns(session_id, limit=2)
        if len(turns) != 1 or not is_generation_current(generation_id):
            return None
        try:
            candidate = await self._backend(
                ThreadTitleRequest(
                    generation_id=generation_id,
                    turn=turns[0],
                    max_characters=self._max_characters,
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A title is never worth failing a turn for, and the default title
            # is already a correct thing to show.
            return None
        title = _clean_title(candidate, self._max_characters)
        if title is None or not is_generation_current(generation_id):
            return None
        try:
            written = self._store.set_generated_conversation_title(session_id, title)
        except ValueError:
            return None
        return title if written else None


def _clean_title(value: str, maximum: int) -> str | None:
    """Reduce free model output to one short printable line, or to nothing."""

    lines = [line for line in value.strip().splitlines() if line.strip()]
    if not lines:
        return None
    collapsed = " ".join(lines[0].split())
    printable = "".join(character for character in collapsed if character.isprintable())
    title = printable.strip(_STRIPPED_EDGES)[:maximum].strip(_STRIPPED_EDGES)
    return title or None
