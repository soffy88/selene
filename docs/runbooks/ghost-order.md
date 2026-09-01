# Ghost order

Trigger: local row without venue, or venue order without local row.
Steps: halt, freeze submits, dump `side_effects` and `orders`. Never place a second order to "sync".
Owner: Soffy.
