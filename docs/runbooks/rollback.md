# Rollback

Trigger: bad release.
Steps: deploy previous immutable tag. Ledger tables must not be truncated. Live modes stay off; rollback lands in PAPER/NOTIFY_ONLY.
Owner: Soffy.
