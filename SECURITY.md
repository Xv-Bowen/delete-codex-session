# Security Policy

## Reporting a vulnerability

Report security issues through a private GitHub security advisory for this repository. Do not open a public issue containing real Codex session IDs, local paths, database contents, approval fingerprints, staged deletion jobs, receipts, or other user data.

Include the smallest synthetic reproduction that demonstrates the problem. For deletion-scope or approval-boundary issues, describe the expected preserved objects and the unexpected mutation or authorization behavior.

## Safety boundary

This project performs permanent local deletion without an automatic backup. A report or test failure must never be treated as deletion authorization. Unknown, ambiguous, protected, or newly discovered objects must remain preserved until the applicable scope is explicitly approved.
