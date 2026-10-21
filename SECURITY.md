# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |

## Reporting a Vulnerability

We take the security and privacy of multi-agent memory and retrieval systems seriously.

If you discover a security vulnerability (such as memory cross-domain leakage, injection guardrail bypass, privilege escalation, or unauthorized memory exposure), please **do not create a public issue**.

### How to Report

1. **Email/Private Disclosure**: Please send security vulnerability reports directly to `maintainers@example.com` (or submit a private GitHub Security Advisory).
2. Include in your report:
   - Type of vulnerability (e.g., Cross-domain recall leakage, prompt injection bypass, SQL/WAL corruption).
   - Minimal reproduction steps, sample scripts, or attack payload.
   - Expected behavior vs. observed behavior.
   - Impact assessment on host Agent platforms.

### Scope and Privacy Guidelines

- **Zero Real Data**: This public repository contains only sanitized schemas, synthetic test suites, and mock data.
- **Do Not Post Secrets/Memory Dumps**: When submitting issues or PRs, never post live API keys, session transcripts, or real production SQLite memory databases.
- Vulnerability fixes will be credited in the security changelog after coordinated disclosure.
