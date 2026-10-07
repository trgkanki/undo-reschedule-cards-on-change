# Undo reschedule cards on change

[![Donate via patreon](https://img.shields.io/badge/patreon-donate-green.svg)](https://www.patreon.com/trgk)

Reverts FSRS's **Reschedule cards on change** for a deck.

`Tools > Undo reschedule cards on change...` lets you pick a date and a deck (or `(All decks)`).
For each card in that deck rescheduled since that date:

- If the card has only "Rescheduled" revlog entries since that date, its interval and due date are restored to what they were before the first reschedule.
- If the card has any other revlog entry (review, manual set due date, ...) since that date, it is skipped.

Revlog entries and FSRS memory state are left untouched. The whole operation is a single step in `Edit > Undo`.
