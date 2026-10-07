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
from aqt.operations import CollectionOp
from aqt.qt import (
    QCalendarWidget,
    QDate,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    Qt,
    QVBoxLayout,
)
from aqt.utils import showInfo

from .undoResched import getCutoffMs, undoReschedule

ALL_DECKS_LABEL = "(All decks)"


class UndoRescheduleDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Undo Reschedule Cards on Change")
        self.resize(400, 560)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("Undo reschedules done since:"))
        self.calendar = QCalendarWidget()
        self.calendar.setMaximumDate(QDate.currentDate())
        self.calendar.setSelectedDate(QDate.currentDate())
        layout.addWidget(self.calendar)

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
        layout.addWidget(self.deckList)

        note = QLabel(
            "Cards rescheduled by 'Reschedule cards on change' since the selected "
            "date get their previous interval and due date back. Cards that were "
            "reviewed (or otherwise changed) since then are skipped."
        )
        note.setWordWrap(True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selectedDeckId(self):
        item = self.deckList.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def daysAgo(self):
        return self.calendar.selectedDate().daysTo(QDate.currentDate())


def undoRescheduleGUI():
    dlg = UndoRescheduleDialog(mw)
    if not dlg.exec():
        return

    deckId = dlg.selectedDeckId()
    cutoffMs = getCutoffMs(mw.col, dlg.daysAgo())

    def onSuccess(result):
        showInfo(
            "[Undo reschedule] Restored %d cards.\n\n"
            "Skipped %d cards reviewed since the date, "
            "%d cards changed by something else (or already restored)."
            % (result.restored, result.skippedReviewed, result.skippedModified),
            parent=mw,
        )

    CollectionOp(
        parent=mw, op=lambda col: undoReschedule(col, deckId, cutoffMs)
    ).success(onSuccess).run_in_background()
