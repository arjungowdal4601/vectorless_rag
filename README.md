# Document Processing

A Python 3.12 service that turns one uploaded PDF into reliable, page-level multimodal knowledge artifacts for a separate indexing layer. It preserves the original evidence, renders every page, analyzes pages sequentially with Deep Agents, and supports cancellation and validated recovery.

This repository intentionally does **not** implement indexing, retrieval, embeddings, vector search, chat, or answer generation.

## Requirements

- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- An OpenAI API key for live page analysis

## Local setup

```bash
cp .env.example .env
# Set OPENAI_API_KEY in .env.
make setup
make run
```

The service listens on `127.0.0.1:8000` by default. Runtime artifacts are written beneath `./var/document-processing` and are excluded from version control.

`make setup`, `make test`, `make lint`, and `make run` all honor `uv.lock`; commands fail if project metadata and the lock disagree. The production dependencies are exact direct pins, including `deepagents==0.7.9`.

Run the deterministic checks with:

```bash
make lint
make test
```

## Python API

The reusable processor and HTTP service use the same durable implementation:

```python
import asyncio
from pathlib import Path

from document_processing import Settings, create_processor

processor = create_processor(Settings())
result = asyncio.run(processor.process_pdf(Path("document.pdf")))
print(result.run_id, result.status, result.manifest_path)
```

`DocumentProcessor.process_pdf` and `DocumentProcessor.resume` are asynchronous because model analysis is asynchronous. `resume(run_id)` audits a failed run before continuing from its first incomplete page.

For an embedded durable queue, `create_service(Settings())` returns a `LocalRunService`. Use it as an async context manager, then call its asynchronous `submit_pdf`, `get_run`, `list_pages`, `cancel`, `resume`, `wait`, and `get_manifest` methods. Contract enums and strict Pydantic artifact models are available from `document_processing.contracts`.

## API

Create a run with exactly one multipart PDF:

```bash
curl -F 'file=@/absolute/path/document.pdf;type=application/pdf' \
  http://127.0.0.1:8000/v1/runs
```

The endpoint returns `202 Accepted`. Use the returned run identifier with the remaining endpoints.

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/v1/runs` | Accept exactly one multipart `file` and start processing. |
| `GET` | `/v1/runs/{run_id}` | Read document and per-run processing state. |
| `GET` | `/v1/runs/{run_id}/pages?offset=0&limit=100` | List page results; `limit` must be from 1 through 500. |
| `GET` | `/v1/runs/{run_id}/manifest` | Read the completed artifact manifest; returns `409` before completion. |
| `POST` | `/v1/runs/{run_id}/cancel` | Request cooperative cancellation between pages or before commit. |
| `POST` | `/v1/runs/{run_id}/resume` | Audit and queue a resumable failed run. |
| `GET` | `/healthz` | Process liveness check. |
| `GET` | `/readyz` | Dependency and storage readiness check. |

API errors use `application/problem+json`. Authentication and CORS are not part of this local service.

The service returns `413` for upload-size violations, `415` for invalid PDF media types, `422` for malformed, encrypted, empty, or configured-limit-exceeding PDFs, `404` for unknown runs, and `409` for illegal lifecycle operations or a manifest requested before completion.

Every accepted upload receives a server-generated UUID run ID. The full source SHA-256 is its `document_id`; uploading the same bytes again intentionally creates another run.

## Configuration

Settings use the `DOCUMENT_PROCESSING_` prefix.

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENAI_API_KEY` | unset | Credential consumed by the OpenAI client; never stored in artifacts. |
| `DOCUMENT_PROCESSING_MODEL` | `gpt-5.6-luna` | Explicit multimodal model used by the page analyzer. |
| `DOCUMENT_PROCESSING_ARTIFACT_ROOT` | `./var/document-processing` | Root for source PDFs, rendered pages, outputs, and recovery state. |
| `DOCUMENT_PROCESSING_HOST` | `127.0.0.1` | Fixed loopback bind; every other value is rejected. |
| `DOCUMENT_PROCESSING_PORT` | `8000` | HTTP bind port. |
| `DOCUMENT_PROCESSING_REASONING_EFFORT` | `medium` | Model reasoning effort: `low`, `medium`, or `high`. |
| `DOCUMENT_PROCESSING_RENDER_DPI` | `200` | Lossless PNG rendering resolution; accepted range is 72–600 DPI. |
| `DOCUMENT_PROCESSING_MAX_UPLOAD_BYTES` | `104857600` | Maximum accepted PDF size in bytes. |
| `DOCUMENT_PROCESSING_MAX_PAGES` | `1000` | Maximum pages accepted in one PDF. |
| `DOCUMENT_PROCESSING_MAX_RENDERED_MEGAPIXELS` | `40.0` | Maximum rendered pixel area for one page. |
| `DOCUMENT_PROCESSING_MODEL_TIMEOUT_SECONDS` | `180.0` | Timeout for one model attempt. |
| `DOCUMENT_PROCESSING_MAX_MODEL_ATTEMPTS` | `3` | Total bounded attempts for transient model failures. |
| `DOCUMENT_PROCESSING_WORKER_QUEUE_CAPACITY` | `100` | Maximum queued local runs. |
| `DOCUMENT_PROCESSING_SHUTDOWN_GRACE_SECONDS` | `30.0` | Grace period for cooperative worker shutdown. |
| `DOCUMENT_PROCESSING_PAGE_LIST_DEFAULT_LIMIT` | `100` | Default page-list response size. |
| `DOCUMENT_PROCESSING_PAGE_LIST_MAX_LIMIT` | `500` | Maximum page-list response size. |

## Processing model

The runtime is a deterministic Python orchestrator around a minimal `deepagents==0.7.9` analyzer. The analyzer is stateless across pages and receives only the current page image, its assigned 1-based page number, and the latest short-term memory. It does not receive earlier page messages, accumulated document structure, filesystem tools, or delegation capabilities. Each invocation also clears inherited callbacks and disables ambient LangChain/LangSmith tracing so inline page-image payloads cannot leak into application tracing.

Application code owns PDF rendering, validation, stable topic and asset identifiers, state transitions, persistence, and atomic page commits. A completed run contains:

- the preserved source PDF;
- one rendered image and one exact-contract JSON artifact for every page;
- final short-term memory and page-ordered document structure;
- per-page memory and structure snapshots;
- status, recovery, and model-usage records;
- a completion manifest that ties page numbers to their evidence paths.

Only transient provider or connection failures are retried. Completed pages are reused during resume, while missing, corrupt, or contradictory state is rejected rather than silently repaired.

Cancellation is cooperative. A cancelled run terminates with status `failed` and failure code `cancelled`; pages without durable commits remain `pending`. The reserved page status `skipped` is never treated as complete.

The durable layout is:

```text
var/document-processing/
  state.sqlite3
  runs/<run_id>/
    source/original.pdf
    run.json
    render_manifest.json
    page_images/page-0001.png
    initial/
    page_artifacts/page-0001/
      page.json
      short_term_memory.json
      document_structure.json
      model_response.json
      usage.json
      commit.json
    final/
      output_manifest.json
      short_term_memory.json
      document_structure.json
      model_usage.json
      processing_status.json
      recovery_events.json
    .staging/
    quarantine/
```

Only `final/output_manifest.json` from a run whose status is `completed` is a downstream indexing boundary. No API keys, base64 page requests, raw reasoning, or provider message history are stored.

## Tests

`make test` runs the deterministic, offline suite and never calls a model provider. It covers contracts, PDF rendering and validation, the real Deep Agents middleware graph with an offline transport, durable commit recovery, lifecycle races, and the HTTP API.

The 14 live model cases are explicitly opt-in and skip unless both `RUN_LIVE_MODEL_TESTS=1` and `OPENAI_API_KEY` are present. They repeat page classification, cross-page continuation, exact-number and negation preservation, prompt-injection resistance, poisoned-memory resistance, and page-evidence-over-memory trials. Run only that suite with:

```bash
RUN_LIVE_MODEL_TESTS=1 uv run --locked pytest tests/analysis/test_live_model.py
```

Never put credentials, raw provider histories, or base64 page payloads in test output or fixtures.

## Repository layout

```text
src/document_processing/  application package
tests/                    deterministic unit and API tests
assets/                   small non-code resources when needed
flow.md                   requested work and current status
vision.md                 durable product direction
changelog.md              completed and verified changes only
```

Generated documents, credentials, indexes, and local environments must not be committed.
