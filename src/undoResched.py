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

import time
from dataclasses import dataclass, field
from itertools import groupby

from anki.cards import Card
from anki.collection import OpChanges
from anki.consts import CARD_TYPE_REV, QUEUE_TYPE_REV
from anki.utils import ids2str

from .utils.debugLog import log

# RevlogReviewKind (rslib/src/revlog/mod.rs).
REVLOG_FILTERED = 3
# Written by 'Set due date' (non-zero factor) and 'Forget' (factor 0).
# Undo rows are written as this kind with non-zero factor, like 'Set due date'.
# FSRS Helper's after-sync reschedule/disperse ignores manual entries.
REVLOG_MANUAL = 4
# Written by FSRS "Reschedule cards on change".
REVLOG_RESCHEDULED = 5


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


def dueTodayCounts(col, plan):
    """(before, after) number of planned cards due today."""
    today = col.sched.today
    reviewQueue = [it for it in plan.items if it.card.queue == QUEUE_TYPE_REV]
    before = sum(1 for it in reviewQueue if cardDue(it.card) <= today)
    after = sum(1 for it in reviewQueue if it.targetDue <= today)
    return before, after


def revlogFactor(card):
    """Same as Anki's log_scheduled_review: FSRS difficulty or SM-2 ease.

    Kept non-zero, as a manual entry with factor 0 means 'Forget'.
    """
    if card.memory_state:
        factor = int(((card.memory_state.difficulty - 1) / 9 + 0.1) * 1000)
    else:
        factor = card.factor
    return max(factor, 1)


def applyUndoReschedule(col, plan):
    """Apply the plan and log each restored card as a manual revlog entry.

    Not undoable: the raw revlog insert clears Anki's undo history.
    """
    if not plan.items:
        return OpChanges()

    usn = col.usn()
    nextId = max(
        int(time.time() * 1000), (col.db.scalar("select max(id) from revlog") or 0) + 1
    )

    cards = []
    revlogRows = []
    for item in plan.items:
        card = item.card
        revlogRows.append(
            (
                nextId,
                card.id,
                usn,
                0,  # ease
                item.targetIvl,
                card.ivl,  # lastIvl
                revlogFactor(card),
                0,  # time
                REVLOG_MANUAL,
            )
        )
        nextId += 1

        card.ivl = item.targetIvl
        if card.odid:
            card.odue = item.targetDue
        else:
            card.due = item.targetDue
        cards.append(card)

    result = {}

    def op():
        result["changes"] = col.update_cards(cards)
        # Must come after update_cards: this raw write clears the undo stack,
        # including update_cards' entry, so Ctrl+Z can't revert cards alone.
        col.db.executemany(
            "insert into revlog (id, cid, usn, ease, ivl, lastIvl, factor, time, type)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            revlogRows,
        )

    col.db.transact(op)

    log(
        "applyUndoReschedule: restored %d, skipped %d changed / %d not review, %d already restored"
        % (
            len(cards),
            plan.skippedChanged,
            plan.skippedNotReview,
            plan.alreadyRestored,
        )
    )
    return result["changes"]
