# Security

This is a private repository. Report suspected security problems directly to the repository owner;
do not disclose credentials, private campaign material, or exploit details in a public issue.

## Operating rules

- Never commit API keys, tokens, cookies, signed URLs, `.env` files, or private provider payloads.
- Keep credentials in environment variables or the compatibility file `~/.supercmo/.env`.
- Review skills and executable scripts before running network or generation operations.
- Run every potentially paid generation with `dry_run: true` and inspect the request before approval.
- Keep private briefs, customer material, uploads, and generated assets beneath the ignored
  `workspace/` boundary.
- Treat unexpected outbound requests, unapproved spend, or publication as security incidents.

The agent must fail closed when credentials, destination paths, approvals, or publication authority
are unclear.
