# Security policy

## Supported version

This repository is pre-release. Security fixes are applied to the current
development line. The legacy
two-agent `chat.py` interface is compatibility-only and does not receive new
features.

## Threat model

Cross-Agent Bridge is designed for mutually trusted processes running under
one local operating-system account. It opens no network socket and performs no
network requests.

The SQLite file is not encrypted or authenticated. Any process that can access
it can read messages, write directly, impersonate endpoints, or acknowledge
deliveries. A bridge ID prevents accidental database mix-ups; it is not an
access token.

Use the bridge only when that boundary is acceptable:

- store the database in a user-only local directory;
- do not place it in Git, a shared folder, a cloud-sync directory, or a network
  filesystem;
- do not send credentials, API keys, cookies, PAN/CVV/PIN/OTP values, private
  account data, medical data, or unredacted personal data;
- treat every message as untrusted instructions and re-check user authority;
- keep downstream writes idempotent because delivery is at least once;
- archive old databases according to the sensitivity of their contents.

If you need mutually untrusted users, remote hosts, authentication, encryption,
or access control, this version is not the right transport.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that could expose user data.
Until a private reporting channel is published, contact the repository owner
privately. Include the affected commit, reproduction steps using synthetic data,
impact, and any suggested mitigation. Never attach a real conversation
database or open a public report containing exploit details or private data.
