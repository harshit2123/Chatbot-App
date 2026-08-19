---
name: verify
description: Run the full check for this repo — backend pytest suite plus frontend TypeScript build — and report every failure with file:line.
---

Run both checks. Do not stop at the first failure; run both, then report.

## 1. Backend tests

Postgres must be up (`docker compose up -d postgres` if it is not — each suite
creates its own database on demand).

```bash
cd backend && .venv/bin/python -m pytest -q
```

Expected: 98 tests, no skips. A skip is a failure signal here — suites create their
databases rather than skipping when one is missing, so a skip means something in
`conftest.py` changed.

## 2. Frontend build

```bash
cd frontend && npm run build
```

This runs `tsc -b` then `vite build`, so it is a type check and a bundle check.

## Reporting

- Every failure as `file:line — what broke`.
- If a test fails, say whether the implementation or the test looks wrong; do not
  edit the test to make it pass unless the test is the thing that is wrong.
- If both checks pass, say so in one line. No summary of what you ran.
