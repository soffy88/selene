# Secret rotation

Trigger: suspected leak or scheduled rotation.
Steps: issue new gateway keys, restart gateway, revoke old. Never put keys in query strings.
Verify: anonymous writes still 401; old key 401.
Owner: Soffy.
