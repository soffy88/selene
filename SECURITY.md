# Security

## Reporting

Do not file live-trading bypasses in public issues if they include secrets or account identifiers. Contact the owner.

## Hard rules

- Production `EXEC_MODE` in `{LIMITED_LIVE, AUTO_EXEC}` refuses to boot without bound artifacts.
- `I_HAVE_OOS_EVIDENCE=yes` is not qualification.
- Production gateway refuses to start without read/operator/admin secrets.
- Write routes never return 2xx anonymously.
- API keys must not appear in query strings, logs, frontend bundles, or `/api/v4/config/*`.
- Venue submits are keyed by `venue + account + client_order_id + operation_kind` and must not retry after a lost ack without a probe.

## Secrets

Use a secrets manager or a root-owned read-only file. Compose interpolates `${}` for passwords; do not commit `.env`. Boot health reports `present/absent` only.
