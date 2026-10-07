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

from dataclasses import dataclass, field
from itertools import groupby

from anki.cards import Card
from anki.collection import OpChanges
from anki.consts import CARD_TYPE_REV
from anki.utils import ids2str

from .utils.debugLog import log

# RevlogReviewKind (rslib/src/revlog/mod.rs).
REVLOG_FILTERED = 3
# Written by 'Set due date' (non-zero factor) and 'Forget' (factor 0).
REVLOG_MANUAL = 4
# Written by FSRS "Reschedule cards on change".
REVLOG_RESCHEDULED = 5


@dataclass
class UndoRescheduleResult:
    changes: OpChanges = None
    restored: int = 0
    skippedReviewed: int = 0
    skippedModified: int = 0


@dataclass
class RestoreItem:
    card: Card
    targetIvl: int
    targetDue: int


@dataclass
class UndoReschedulePlan:
    items: list = field(default_factory=list)
    skippedChanged: int = 0
    skippedNotReview: int = 0
    alreadyRestored: int = 0


def getCutoffMs(col, daysAgo):
    """Start of the Anki day (honoring rollover) `daysAgo` days before today."""
    return (col.sched.day_cutoff - 86400 * (daysAgo + 1)) * 1000


def deckFilterSql(col, deckId):
    """SQL fragment restricting revlog rows to deckId and its subdecks.

    deckId=None means all decks.
    """
    if deckId is None:
        return ""
    dids = ids2str(col.decks.deck_and_child_ids(deckId))
    return f" and cid in (select id from cards where did in {dids} or odid in {dids})"


def getRescheduleCountsByDay(col, deckId):
    """{daysAgo: number of cards rescheduled on that Anki day}"""
    dayEndMs = col.sched.day_cutoff * 1000
    return dict(
        col.db.all(
            "select (? - id - 1) / 86400000 as daysAgo, count(distinct cid)"
            " from revlog where type = ?"
            + deckFilterSql(col, deckId)
            + " group by daysAgo",
            dayEndMs,
            REVLOG_RESCHEDULED,
        )
    )


def getRescheduledCardCount(col, deckId, cutoffMs):
    """Number of cards rescheduled since cutoffMs."""
    return col.db.scalar(
        "select count(distinct cid) from revlog where type = ? and id >= ?"
        + deckFilterSql(col, deckId),
        REVLOG_RESCHEDULED,
        cutoffMs,
    )


def getLastReviewSecs(revlogRows):
    """Time of the last rating that affects scheduling, or None.

    Mirrors get_last_revlog_info (rslib/src/scheduler/fsrs/memory_state.rs).
    """
    lastReview = None
    for rid, rType, ease, _, _, factor in revlogRows:
        if 1 <= ease <= 4 and not (rType == REVLOG_FILTERED and factor == 0):
            lastReview = rid // 1000
        elif rType == REVLOG_MANUAL and factor == 0:
            # 'Forget' resets the card
            lastReview = None
    return lastReview


def cardDue(card):
    return card.odue if card.odid else card.due


def planUndoReschedule(col, deckId, cutoffMs):
    """Plan restoring cards rescheduled since cutoffMs to their state before that.

    deckId=None means all decks. Subdecks are included.
    The target interval is lastIvl of the first reschedule since cutoff, whatever
    happened to the card's interval since (e.g. unlogged changes by other add-ons).
    Cards with any non-reschedule revlog entry since cutoff are skipped.
    """
    plan = UndoReschedulePlan()

    revlogs = col.db.all(
        "select cid, id, type, ease, ivl, lastIvl, factor from revlog"
        " where cid in (select distinct cid from revlog where type = ? and id >= ?"
        + deckFilterSql(col, deckId)
        + ") order by cid, id",
        REVLOG_RESCHEDULED,
        cutoffMs,
    )

    for cid, group in groupby(revlogs, key=lambda r: r[0]):
        rows = [r[1:] for r in group]
        sinceCutoff = [r for r in rows if r[0] >= cutoffMs]
        if any(rType != REVLOG_RESCHEDULED for _, rType, *_ in sinceCutoff):
            plan.skippedChanged += 1
            continue

        try:
            card = col.get_card(cid)
        except Exception:
            # Card might have been deleted after rescheduling
            continue

        targetIvl = sinceCutoff[0][4]
        if card.type != CARD_TYPE_REV or targetIvl <= 0:
            # lastIvl < 0 means learning step (seconds); shouldn't happen for review cards.
            plan.skippedNotReview += 1
            continue

        lastReviewSecs = getLastReviewSecs(rows)
        if lastReviewSecs is None:
            targetDue = cardDue(card) + targetIvl - card.ivl
        else:
            daysElapsed = max(col.sched.day_cutoff - lastReviewSecs, 0) // 86400
            targetDue = col.sched.today - daysElapsed + targetIvl

        if card.ivl == targetIvl and cardDue(card) == targetDue:
            plan.alreadyRestored += 1
            continue

        plan.items.append(RestoreItem(card, targetIvl, targetDue))

    return plan


def undoReschedule(col, deckId, cutoffMs):
    """Revert 'reschedule cards on change' done since cutoffMs.

    deckId=None means all decks. Subdecks are included.
    Cards with any non-reschedule revlog entry since cutoff are skipped.
    """
    result = UndoRescheduleResult()

    cardIds = col.db.list(
        "select distinct cid from revlog where type = ? and id >= ?"
        + deckFilterSql(col, deckId),
        REVLOG_RESCHEDULED,
        cutoffMs,
    )

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
