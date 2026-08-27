# Repository Guidelines

## Project Structure & Module Organization

This repository contains the `document_processing` Python package and its local FastAPI service. Keep configuration and documentation at the root, application code under `src/`, tests under `tests/`, and non-code resources under `assets/`. Organize code by domain, as in `src/document_processing/pdf/`, `processing/`, `storage/`, and `service/`, rather than collecting unrelated helpers in `src/utils/`. Place small fixtures beside the relevant tests or in `tests/fixtures/`; do not commit generated artifacts, indexes, sensitive source data, credentials, or local environments.

## Build, Test, and Development Commands

The project uses Python 3.12, `uv.lock`, and a root Makefile. Use these predictable, non-interactive commands:

- `make setup` — install dependencies and initialize tooling.
- `make test` — run the complete automated test suite.
- `make lint` — run formatting, linting, and static checks.
- `make run` — start the application locally.

## Coding Style & File Size

Commit formatter and linter configuration with the first implementation. Use spaces, UTF-8, trailing newlines, descriptive domain names, `snake_case` for Python files and functions, `PascalCase` for classes, and `UPPER_SNAKE_CASE` for constants.

Every code-bearing source, test, script, or configuration file must contain no more than 350 physical lines. Files below 300 lines are valid; 300 is not a minimum. Split files by responsibility before they exceed the limit. Markdown documentation, generated output, vendored dependencies, and lockfiles are exempt.

## Project Records

Maintain three root-level records. They answer different questions and must not duplicate one another as interchangeable activity logs.

### `flow.md`: What Was Requested?

Use `flow.md` as the chronological work-request log. Record user prompts that request, clarify, constrain, or change repository work. Convert each request into a concise, actionable paraphrase; do not preserve raw transcripts, greetings, status questions, or unrelated conversation.

Each entry must contain:

- a date and stable ID such as `FLOW-2026-001`;
- the requested outcome and relevant acceptance criteria;
- important constraints or clarifications;
- a status: `planned`, `in-progress`, `completed`, `rejected`, or `superseded`;
- links to related vision or changelog entries when applicable.

Update the entry when its status changes instead of creating duplicate requests.

### `vision.md`: Why and Where Are We Going?

Use `vision.md` as the current source of truth for product intent. Record the problem being solved, target users, value proposition, product principles, boundaries, major capabilities, and long-term direction communicated by the user. Do not include task status, file-level implementation steps, or routine bug fixes.

Keep the main sections aligned with the latest direction. When a significant product decision is replaced, move its summary to a `Vision Revisions` section with the date, related flow ID, replacement decision, and `superseded` label. This preserves history without leaving contradictory guidance active.

### `changelog.md`: What Was Completed?

Use `changelog.md` only for completed and verified product or implementation changes. Group entries by date or release and explain user-visible behavior, important technical changes, migrations, and compatibility effects. Every entry must link to its flow ID when one exists. Do not record planned, in-progress, rejected, or speculative work here; those states belong in `flow.md`.

### Update and Ownership Rules

Update records at these points:

1. Add or revise `flow.md` when the user provides a work request or clarification.
2. Also update `vision.md` when the request changes product purpose, audience, principles, boundaries, or direction.
3. After implementation and verification, add the result to `changelog.md` and mark the corresponding flow entry `completed`.

A request may affect all three records, but each fact has one authoritative home: request and status in `flow.md`, durable product intent in `vision.md`, and verified outcome in `changelog.md`. Cross-reference shared work with the same flow ID.

### Required Formats

Use this entry shape in `flow.md`:

```markdown
## FLOW-2026-001 — Add document ingestion
- Date: 2026-08-26
- Request: Accept and process uploaded documents.
- Acceptance criteria: Supported files become searchable.
- Constraints: Processing must remain offline.
- Status: planned
- References: Vision — Major Capabilities
```

Keep `vision.md` organized under `Product Purpose`, `Target Users`, `Value Proposition`, `Principles`, `Boundaries`, `Major Capabilities`, `Long-Term Direction`, and `Vision Revisions`. Write current guidance as direct statements, for example: “The product must answer questions from user-provided documents without requiring a vector database.”

Use dated or released sections in `changelog.md` with categories such as `Added`, `Changed`, `Fixed`, and `Removed`. Example: `- [FLOW-2026-001] Added offline ingestion for PDF documents.`

## Testing Guidelines

Add tests for every behavior change or bug fix. Mirror source paths beneath `tests/`, name Python files `test_<module>.py`, and name cases `test_<behavior>`. Keep unit tests deterministic and offline; isolate external services behind clearly marked integration fixtures.

## Commits, Pull Requests & Security

Use concise, imperative commits, optionally Conventional Commits, such as `feat: add document chunker`. Pull requests must explain the problem and solution, list verification commands, link issues, and call out configuration or schema changes. Store secrets in environment variables, provide sanitized examples in `.env.example`, and never commit keys or production logs.
