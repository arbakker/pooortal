# pooortal

**pooortal** is a web application that allows administrators to generate shareable, optionally time-limited URLs granting external (unauthenticated) users access to a file upload form. Uploaded files are stored via the [Tus](https://tus.io/) resumable upload protocol backed by Azure Blob Storage and are subsequently processed asynchronously in the background.

## Implementation

The application is a **single Go binary** that embeds [tusd](https://github.com/tus/tusd) (`github.com/tus/tusd/v2`) as a library. This means:

- No separate tusd process — the Tus upload endpoint is served directly by the Go app.
- No HTTP hook callbacks or shared secrets — upload hooks (`PreCreate`, `PostFinish`) are in-process Go functions.
- A single container image serves the admin API, upload pages, Tus endpoint, and SSE result delivery.

## Features

- **Admin interface** — create and manage shareable upload URLs with configurable constraints (expiry, max uses, file type/size limits).
- **Resumable uploads** — [tus-js-client](https://github.com/tus/tus-js-client) in the browser, tusd embedded in the Go server, streaming directly to Azure Blob Storage.
- **Metadata enrichment** — the `PreCreate` hook injects server-authoritative metadata (`token_label`, `token_id`, `created_by_admin`) into each upload's `.info` file.
- **Background processing** — pluggable processors (`noop`, `virus_scan`, `metadata_extract`, `custom_webhook`) run in goroutine worker pools.
- **Real-time results** — Server-Sent Events (SSE) push processing results back to the uploading client.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Go (`net/http` + router) |
| Tus server | `github.com/tus/tusd/v2` (embedded) |
| Storage | Azure Blob Storage (Azurite for local dev) |
| Database | PostgreSQL 15+ |
| Real-time | Server-Sent Events (SSE) |
| Frontend | tus-js-client, React or HTMX |
| Deployment | Kubernetes (AKS) with Workload Identity |

## Documentation

See [SPEC.md](./SPEC.md) for the full technical specification.

## Quick Start

```bash
# Start dependencies
docker compose up -d postgres azurite

# Run the server (includes embedded worker goroutines)
go run ./cmd/server

# Open the admin UI
open http://localhost:8080/admin
```
