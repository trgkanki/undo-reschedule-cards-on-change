# Copyright (C) 2020 Hyun Woo Park
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from dataclasses import dataclass

from anki.collection import OpChanges
from anki.consts import CARD_TYPE_REV
from anki.utils import ids2str

from .utils.debugLog import log

# RevlogReviewKind::Rescheduled (rslib/src/revlog/mod.rs).
# Written by FSRS "Reschedule cards on change".
REVLOG_RESCHEDULED = 5


@dataclass
class UndoRescheduleResult:
    changes: OpChanges = None
    restored: int = 0
    skippedReviewed: int = 0
    skippedModified: int = 0


def getCutoffMs(col, daysAgo):
    """Start of the Anki day (honoring rollover) `daysAgo` days before today."""
    return (col.sched.day_cutoff - 86400 * (daysAgo + 1)) * 1000


def undoReschedule(col, deckId, cutoffMs):
    """Revert 'reschedule cards on change' done since cutoffMs.

    deckId=None means all decks. Subdecks are included.
    Cards with any non-reschedule revlog entry since cutoff are skipped.
    """
    result = UndoRescheduleResult()

    cardIds = col.db.list(
        "select distinct cid from revlog where type = ? and id >= ?",
        REVLOG_RESCHEDULED,
        cutoffMs,
    )
    if deckId is not None:
        dids = ids2str(col.decks.deck_and_child_ids(deckId))
        deckCardIds = set(
            col.db.list(f"select id from cards where did in {dids} or odid in {dids}")
        )
        cardIds = [cid for cid in cardIds if cid in deckCardIds]

    changedCards = []
    for cid in cardIds:
        rows = col.db.all(
            "select type, ivl, lastIvl from revlog where cid = ? and id >= ? order by id",
            cid,
            cutoffMs,
        )
        if any(rType != REVLOG_RESCHEDULED for rType, _, _ in rows):
            result.skippedReviewed += 1
            continue

        try:
            card = col.get_card(cid)
        except Exception:
            # Card might have been deleted after rescheduling
            continue

        lastIvl = rows[-1][1]
        if card.type != CARD_TYPE_REV or card.ivl != lastIvl:
            # Changed by something else since, or already undone.
            result.skippedModified += 1
            continue

        origIvl = rows[0][2]
        if origIvl <= 0:
            # lastIvl < 0 means learning step (seconds); shouldn't happen for review cards.
            result.skippedModified += 1
            continue

        shift = origIvl - card.ivl
        if card.odid:
            card.odue += shift
        else:
            card.due += shift
        card.ivl = origIvl
        changedCards.append(card)

    log(
        "undoReschedule(%s, %d): restored %d, skipped %d reviewed / %d modified"
        % (
            deckId,
            cutoffMs,
            len(changedCards),
            result.skippedReviewed,
            result.skippedModified,
        )
    )

    undoEntry = col.add_custom_undo_entry("Undo Reschedule Cards on Change")
    if changedCards:
        col.update_cards(changedCards)
    result.changes = col.merge_undo_entries(undoEntry)
    result.restored = len(changedCards)
    return result
