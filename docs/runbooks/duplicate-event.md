# Duplicate event

Trigger: Redis XREADGROUP redelivery.
Checks: `client_order_id` already in `side_effects`.
Steps: ack the stream message; do not call place_order.
Owner: Soffy.
