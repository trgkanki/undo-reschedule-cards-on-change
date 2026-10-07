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

from aqt import mw
from aqt.operations import CollectionOp, QueryOp
from aqt.qt import (
    QCalendarWidget,
    QDate,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPalette,
    Qt,
    QVBoxLayout,
)
from aqt.utils import askUser, showInfo

from .undoResched import (
    applyUndoReschedule,
    dueTodayCounts,
    getCutoffMs,
    getRescheduleCountsByDay,
    getRescheduledCardCount,
    planUndoReschedule,
)

import datetime

ALL_DECKS_LABEL = "(All decks)"


def ankiToday():
    """Today's date as Anki sees it (honoring rollover hour)."""
    d = datetime.date.fromtimestamp(mw.col.sched.day_cutoff - 86400)
    return QDate(d.year, d.month, d.day)


class RescheduleCalendar(QCalendarWidget):
    """Calendar showing the number of rescheduled cards in each day cell."""

    def __init__(self):
        super().__init__()
        self.counts = {}  # julian day -> count

    def setCounts(self, counts):
        self.counts = counts
        self.updateCells()

    def paintCell(self, painter, rect, date):
        super().paintCell(painter, rect, date)
        count = self.counts.get(date.toJulianDay())
        if not count:
            return

        painter.save()
        font = painter.font()
        font.setPointSizeF(font.pointSizeF() * 0.7)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(self.palette().color(QPalette.ColorRole.Link))
        painter.drawText(
            rect.adjusted(2, 1, -3, -1),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
            str(count),
        )
        painter.restore()


class UndoRescheduleDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Undo Reschedule Cards on Change")
        self.resize(400, 560)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Target deck (subdecks included):"))
        self.deckList = QListWidget()
        allItem = QListWidgetItem(ALL_DECKS_LABEL)
        allItem.setData(Qt.ItemDataRole.UserRole, None)
        self.deckList.addItem(allItem)
        for deck in mw.col.decks.all_names_and_ids(skip_empty_default=True):
            item = QListWidgetItem(deck.name)
            item.setData(Qt.ItemDataRole.UserRole, deck.id)
            self.deckList.addItem(item)
        self.deckList.setCurrentRow(0)
        self.deckList.itemDoubleClicked.connect(self.accept)
        self.deckList.currentRowChanged.connect(self.updateCounts)
        layout.addWidget(self.deckList)

        layout.addWidget(QLabel("Undo reschedules done on or after:"))
        self.calendar = RescheduleCalendar()
        self.calendar.setMaximumDate(ankiToday())
        self.calendar.setSelectedDate(ankiToday())
        self.calendar.selectionChanged.connect(self.updateSummary)
        layout.addWidget(self.calendar)

        self.summary = QLabel()
        layout.addWidget(self.summary)

        note = QLabel(
            "Cards rescheduled by 'Reschedule cards on change' on or after the "
            "selected date get the interval they had before that, with the due date "
            "counted from their last review. Cards reviewed or manually rescheduled "
            "since then are skipped. Each restored card is logged as a manual "
            "entry in its review history.<br><br>"
            "<b>This can't be undone with Ctrl+Z.</b> A backup is created first."
        )
        note.setTextFormat(Qt.TextFormat.RichText)
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.updateCounts()

    def updateCounts(self):
        todayJd = ankiToday().toJulianDay()
        countsByDay = getRescheduleCountsByDay(mw.col, self.selectedDeckId())
        self.calendar.setCounts(
            {todayJd - daysAgo: count for daysAgo, count in countsByDay.items()}
        )
        self.updateSummary()

    def updateSummary(self):
        date = self.calendar.selectedDate()
        dateStr = date.toString(Qt.DateFormat.ISODate)
        countOnDay = self.calendar.counts.get(date.toJulianDay(), 0)
        countSince = getRescheduledCardCount(
            mw.col, self.selectedDeckId(), getCutoffMs(mw.col, self.daysAgo())
        )
        self.summary.setText(
            "Rescheduled on %s: %d cards\n"
            "Rescheduled on or after %s: %d cards"
            % (dateStr, countOnDay, dateStr, countSince)
        )

    def selectedDeckId(self):
        item = self.deckList.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def daysAgo(self):
        return self.calendar.selectedDate().daysTo(ankiToday())


def undoRescheduleGUI():
    dlg = UndoRescheduleDialog(mw)
    if not dlg.exec():
        return

    deckId = dlg.selectedDeckId()
    cutoffMs = getCutoffMs(mw.col, dlg.daysAgo())
    dateStr = dlg.calendar.selectedDate().toString(Qt.DateFormat.ISODate)

    def plan(col):
        p = planUndoReschedule(col, deckId, cutoffMs)
        return p, dueTodayCounts(col, p)

    QueryOp(
        parent=mw,
        op=plan,
        success=lambda result: confirmAndApply(dateStr, *result),
    ).with_progress().run_in_background()


def skippedHtml(plan, dateStr):
    return (
        "Skipped:"
        "<br>&nbsp;&nbsp;- %d reviewed or manually changed since %s"
        "<br>&nbsp;&nbsp;- %d not in review state"
        "<br>&nbsp;&nbsp;- %d already restored"
        % (plan.skippedChanged, dateStr, plan.skippedNotReview, plan.alreadyRestored)
    )


def confirmAndApply(dateStr, plan, dueCounts):
    if not plan.items:
        showInfo(
            "<p>Nothing to restore.</p><p>%s</p>" % skippedHtml(plan, dateStr),
            parent=mw,
            textFormat="rich",
        )
        return

    dueBefore, dueAfter = dueCounts
    if not askUser(
        "<p>Restore %d cards to their state before %s.<br>"
        "Due today among them: %d → %d</p>"
        "<p>%s</p>"
        "<p><b>This can't be undone with Ctrl+Z, and it clears Anki's undo history.</b><br>"
        "A backup is created first.</p>"
        "<p>Continue?</p>"
        % (len(plan.items), dateStr, dueBefore, dueAfter, skippedHtml(plan, dateStr)),
        parent=mw,
        defaultno=True,
        title="Undo Reschedule Cards on Change",
    ):
        return

    def apply(col):
        mw.create_backup_now()
        return applyUndoReschedule(col, plan)

    def onSuccess(_):
        showInfo(
            "<p>Restored %d cards to their state before %s.</p><p>%s</p>"
            % (len(plan.items), dateStr, skippedHtml(plan, dateStr)),
            parent=mw,
            textFormat="rich",
        )

    CollectionOp(parent=mw, op=apply).success(onSuccess).run_in_background()
