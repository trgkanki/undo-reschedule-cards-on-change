# Undo reschedule cards on change

[![Donate via patreon](https://img.shields.io/badge/patreon-donate-green.svg)](https://www.patreon.com/trgk)

Reverts FSRS's **Reschedule cards on change** for a deck.

`Tools > Undo reschedule cards on change...` lets you pick a date and a deck (or `(All decks)`).
For each card in that deck rescheduled since that date:

- If the card has only "Rescheduled" revlog entries since that date, its interval is restored to what it was before the first reschedule, and its due date is counted from its last review. This holds even if something changed the interval since without logging it, so such changes are overwritten. That includes FSRS Helper's after-sync reschedule and disperse, but also its Postpone, Advance, Flatten and Easy Days, none of which write revlog entries.
- If the card has any other revlog entry (review, manual set due date, a previous undo, ...) since that date, it is skipped.

Each restored card gets a "Manual" revlog entry, like `Set due date`. FSRS memory state is left untouched.

A confirmation shows how many cards will be restored and how many will be due today, before and after.

**This can't be undone with `Edit > Undo`**: writing revlog entries clears Anki's undo history. A collection backup is created right before applying.

## TODO

- Restore the exact original due date. It is currently recomputed as last review + restored interval, which differs from the original if the due date had been moved without changing the interval before the reschedule (e.g. `Set due date` without `!`).
