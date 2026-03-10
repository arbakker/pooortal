# pooortal — Technical Specification

**Version:** 2.0.0
**Date:** 2026-03-10
**Author:** arbakker
**Status:** Draft

---

## Table of Contents

1. [Overview](#1-overview)
2. [Goals & Non-Goals](#2-goals--non-goals)
3. [System Architecture](#3-system-architecture)
4. [User Roles](#4-user-roles)
5. [Feature Specifications](#5-feature-specifications)
   - [5.1 Admin Interface](#51-admin-interface)
   - [5.2 Shareable Upload URLs](#52-shareable-upload-urls)
   - [5.3 File Upload Form / Tus Integration](#53-file-upload-form--tus-integration)
   - [5.4 Go ↔ tusd In-Process Integration](#54-go--tusd-in-process-integration)
   - [5.5 Azure Blob Storage via tusd](#55-azure-blob-storage-via-tusd)
   - [5.6 Background Processing](#56-background-processing)
   - [5.7 Result Communication](#57-result-communication)
6. [Data Models](#6-data-models)
7. [API Specification](#7-api-specification)
8. [Technology Stack](#8-technology-stack)
9. [Kubernetes Deployment](#9-kubernetes-deployment)
10. [Security Considerations](#10-security-considerations)
11. [Non-Functional Requirements](#11-non-functional-requirements)
12. [Local Development](#12-local-development)
13. [Open Questions](#13-open-questions)

---

## 1. Overview

**pooortal** is a web application that allows administrators to generate shareable, optionally time-limited URLs granting external (unauthenticated) users access to a file upload form. Uploaded files are stored via a [Tus](https://tus.io/) resumable upload server backed by Azure Blob Storage and are subsequently processed asynchronously in the background. Once processing is complete, the result is communicated back to the uploading client in real time via Server-Sent Events (SSE).

The application is implemented as a **single Go binary** that embeds tusd as a library (`github.com/tus/tusd/v2`). This eliminates the need for a separate tusd process, reverse proxy, HTTP hooks, and shared secrets — all hook logic runs as in-process Go functions.

### High-Level User Journey

```
Admin ──► Creates shareable link (label, expiry, max_uses, file constraints)
               │
               └──► Share URL sent to recipient
                          │
                          ▼
          Recipient opens URL ──► Upload page loads
                          │
                          ▼
          Fills in metadata form ──► Selects file(s)
                          │
                          ▼
          tus-js-client uploads file chunks ──► Go app (tusd embedded) ──► Azure Blob Storage
                          │
                          ▼
          PostFinish hook (in-process) ──► Go app records upload, enqueues job
                          │
                          ▼
          Worker goroutine processes file (reads from Azure Blob Storage)
                          │
                          ▼
          Result pushed to client via SSE ──► Upload page displays result
```

---

## 2. Goals & Non-Goals

### Goals

- Provide a secure admin interface for generating and managing shareable upload URLs.
- Allow external users (no account required) to upload large files reliably using the Tus resumable upload protocol.
- Stream uploads directly to Azure Blob Storage through tusd with no full-file disk buffering.
- Process uploaded files asynchronously in the background without blocking the uploading client.
- Communicate processing results back to the uploading client in real time via SSE.
- Deploy on Kubernetes (AKS) with independent scaling of each component.
- Support passwordless authentication to Azure Blob Storage via AKS Workload Identity.

### Non-Goals

- General-purpose file storage or CDN functionality.
- End-user authentication or registration (uploaders are anonymous, token-authenticated only).
- Video streaming or media transcoding (may be added as a pluggable processor type later).
- Mobile native applications (web only for v1).
- Multi-tenancy with tenant isolation at the database level.

---

## 3. System Architecture

```
┌───────────────────────────────────────────────────────────────────────────┐
│                          Internet / External Users                        │
└──────────────────────────┬──────────────────┬─────────────────────────────┘
                           │ HTTPS             │ HTTPS
                    ┌──────▼──────┐     ┌──────▼──────┐
                    │  Admin SPA  │     │  Upload Page │
                    │  (browser)  │     │  (browser)   │
                    └──────┬──────┘     └──────┬───────┘
                           │ REST/JSON          │ Tus + SSE
                           ▼                    ▼
        ┌──────────────────────────────────────────────────────┐
        │                    Go App (net/http)                 │
        │  /admin/*          Admin API                         │
        │  /upload/<token>   Serve upload page                 │
        │  /upload/tus/*     tusd.Handler (embedded)           │
        │  /events/<token>   SSE endpoint                      │
        │                                                      │
        │  ┌─────────────────────────────────────────────┐    │
        │  │  tusd.Handler (github.com/tus/tusd/v2)      │    │
        │  │  PreCreate hook  ──► validate token/MIME    │    │
        │  │  PostFinish hook ──► record upload, enqueue │    │
        │  └─────────────────────────────────────────────┘    │
        │                                                      │
        │  ┌─────────────────────────────────────────────┐    │
        │  │  Worker goroutines (in-process or separate) │    │
        │  └─────────────────────────────────────────────┘    │
        └──────────────┬──────────────────────┬───────────────┘
                       │                       │ Streams chunks
                       ▼                       ▼
             ┌──────────────────┐   ┌─────────────────────┐
             │   PostgreSQL DB  │   │  Azure Blob Storage │
             │  (tokens, jobs,  │   │  (tusd native)      │
             │   uploads)       │   └─────────────────────┘
             └──────────────────┘
```

### Component Overview

| Component | Technology | Responsibility |
|-----------|-----------|----------------|
| Go app | Go (`net/http` + router) | Admin API, upload page, embedded tusd handler, SSE, in-process hooks |
| tusd (embedded) | `github.com/tus/tusd/v2` | Tus resumable upload protocol, Azure Blob Storage streaming |
| Worker | Go goroutines (in-process or separate binary with `--worker` flag) | Background file processing |
| PostgreSQL | PostgreSQL 15+ | Persistent data (tokens, uploads, jobs, admins) |
| Redis | Redis 7+ (optional) | SSE pub/sub channel across replicas |
| Azure Blob Storage | Azure / Azurite | Durable file storage |

---

## 4. User Roles

| Role | Description | Authentication |
|------|-------------|----------------|
| **Admin** | Creates and manages shareable URLs, views upload/job status, configures constraints | Username + password session (or SSO) |
| **Uploader** | External user who follows a shareable URL and uploads file(s) | None — share token in URL grants access |
| **Worker** | Internal background goroutine or process; consumes jobs from queue and processes uploads | Internal only (no HTTP auth) |

---

## 5. Feature Specifications

### 5.1 Admin Interface

#### 5.1.1 Authentication

- Admins log in with username + password.
- Sessions are server-side with CSRF protection, or stateless JWT (short expiry + refresh token).
- Passwords stored as bcrypt hashes (cost factor ≥ 12).
- Brute-force protection: account lockout after 10 failed attempts within 5 minutes.
- Optional SSO/OAuth2 integration for future iterations.

#### 5.1.2 Dashboard

The admin dashboard provides a summary view:

| Section | Description |
|---------|-------------|
| Active Links | Count and list of currently valid shareable links |
| Recent Uploads | Recent file uploads with processing status |
| Processing Queue | Number of pending / running / failed jobs |

#### 5.1.3 Create Shareable URL

Admins create a new shareable upload URL with the following parameters:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `label` | string | No | Human-readable name for internal reference |
| `expires_at` | datetime (ISO 8601) | No | Expiry timestamp; `null` = never expires |
| `max_uses` | integer | No | Maximum number of completed uploads allowed; `null` = unlimited |
| `allowed_file_types` | string[] | No | MIME type allowlist, e.g. `["image/png", "application/pdf"]` |
| `max_file_size_bytes` | integer | No | Per-file size limit in bytes; `null` = server default (50 GB) |
| `metadata_schema` | JSON Schema | No | Optional JSON Schema for additional uploader-provided metadata fields |
| `notify_email` | email | No | Admin email address to notify on upload completion |

On creation, a cryptographically random **share token** is generated (32 bytes, URL-safe base64, using Go's `crypto/rand`) and a full URL is returned:

```
https://<host>/upload/<share_token>
```

#### 5.1.4 Manage Shareable URLs

- List all tokens (paginated, filterable by status: active / expired / revoked / exhausted).
- View detail: token value, constraints, `use_count`, list of associated uploads.
- Revoke a token immediately (sets `revoked_at`).
- Delete a token (soft delete; uploads are retained).

---

### 5.2 Shareable Upload URLs

#### 5.2.1 Token Structure

| Property | Value |
|----------|-------|
| Format | URL-safe base64, 32 random bytes (43 characters) |
| Generation | `crypto/rand` (Go standard library) |
| Entropy | 256 bits |
| URL path | `/upload/<token>` |

#### 5.2.2 Validation Rules

A share token is considered **valid** when all of the following are true:

1. Token exists in the database.
2. `revoked_at` is `NULL`.
3. `expires_at` is `NULL` or in the future.
4. `max_uses` is `NULL` or `use_count < max_uses`.

On any invalid token request, the server returns HTTP 404 (not 403, to avoid information disclosure).

#### 5.2.3 Use Counting

- `use_count` is incremented atomically (database-level) in the `PostFinish` tusd hook handler, only after a file has been fully and successfully uploaded.
- The `PreCreate` hook checks `use_count < max_uses` to reject new uploads when the token is exhausted.

---

### 5.3 File Upload Form / Tus Integration

#### 5.3.1 Upload Page UI

The upload page is served by the Go app at `/upload/<token>` and renders:

1. A metadata form (fields defined by the token's `metadata_schema`).
2. A file picker (single file for v1; see Open Questions for multi-file).
3. An upload progress bar (driven by tus-js-client events).
4. A status area that displays SSE-pushed results after the upload completes.

#### 5.3.2 tus-js-client Configuration

```javascript
const upload = new tus.Upload(file, {
  endpoint: "/upload/tus/files/",
  retryDelays: [0, 1000, 3000, 5000],
  chunkSize: 50 * 1024 * 1024,  // 50 MB chunks
  metadata: {
    filename: file.name,
    filetype: file.type,
    share_token: shareToken,     // validated server-side in PreCreate hook
    ...userMetadata,
  },
  onProgress(bytesUploaded, bytesTotal) { /* update progress bar */ },
  onSuccess() { /* connect SSE, wait for result */ },
  onError(error) { /* display error */ },
});
```

#### 5.3.3 File Validation

Client-side validation (UX only — server enforcement is authoritative):

- File MIME type against `allowed_file_types`.
- File size against `max_file_size_bytes`.

Server-side enforcement occurs in the tusd `PreCreate` hook (see §5.4.2).

#### 5.3.4 Resumability

- tus-js-client stores the upload URL in `localStorage` keyed by file fingerprint.
- If the user refreshes the page or loses connectivity, resuming is automatic.
- Incomplete uploads on the server expire after 24 hours (tusd `WithExpiration` option).

---

### 5.4 Go ↔ tusd In-Process Integration

The Go application imports tusd as a Go library and creates a `tusd.Handler` that is mounted directly into the HTTP router. There is no separate tusd process, no reverse proxy, and no HTTP hook callbacks — all hook logic is implemented as in-process Go functions.

#### 5.4.1 Handler Setup

```go
import (
    "github.com/tus/tusd/v2/pkg/azurestore"
    "github.com/tus/tusd/v2/pkg/handler"
)

azureService, _ := azurestore.NewAzureService(&azurestore.AzureConfig{
    AccountName:   os.Getenv("AZURE_STORAGE_ACCOUNT"),
    ContainerName: "uploads",
    ObjectPrefix:  "uploads/",
})
store, _ := azurestore.New(azureService)

composer := handler.NewStoreComposer()
store.UseIn(composer)

tusdConfig := handler.Config{
    BasePath:                "/upload/tus/",
    StoreComposer:           composer,
    PreUploadCreateCallback:  preCreateHook,
    PostUploadFinishCallback: postFinishHook,
}

tusdHandler, _ := handler.NewHandler(tusdConfig)
mux.Handle("/upload/tus/", http.StripPrefix("/upload/tus/", tusdHandler))
```

#### 5.4.2 `PreCreate` Hook

Triggered in-process before tusd creates a new upload resource. The hook function validates the upload and enriches server-authoritative metadata, addressing [issue #2](https://github.com/arbakker/pooortal/issues/2).

```go
func preCreateHook(ctx context.Context, event handler.HookEvent) (handler.HookResponse, error) {
    meta := event.Upload.MetaData

    // 1. Validate share token
    token, err := db.GetShareToken(ctx, meta["share_token"])
    if err != nil || !token.IsValid() {
        return handler.HookResponse{
            HTTPResponse: handler.HTTPResponse{StatusCode: http.StatusForbidden},
        }, nil
    }

    // 2. Check use count
    if token.MaxUses != nil && token.UseCount >= *token.MaxUses {
        return handler.HookResponse{
            HTTPResponse: handler.HTTPResponse{StatusCode: http.StatusForbidden},
        }, nil
    }

    // 3. Validate MIME type
    if !token.AllowsMIMEType(meta["filetype"]) {
        return handler.HookResponse{
            HTTPResponse: handler.HTTPResponse{StatusCode: http.StatusUnsupportedMediaType},
        }, nil
    }

    // 4. Validate file size
    if token.MaxFileSizeBytes != nil && event.Upload.Size > *token.MaxFileSizeBytes {
        return handler.HookResponse{
            HTTPResponse: handler.HTTPResponse{StatusCode: http.StatusRequestEntityTooLarge},
        }, nil
    }

    // 5. Inject server-authoritative metadata into the upload's .info file
    return handler.HookResponse{
        ChangeFileInfo: handler.FileInfoChanges{
            MetaData: handler.MetaData{
                "token_label":       token.Label,
                "token_id":          token.ID.String(),
                "created_by_admin":  token.CreatedByAdminEmail,
            },
        },
    }, nil
}
```

Returning a non-2xx `HTTPResponse` causes tusd to abort the upload and return the error to the client.

**Metadata enrichment** (issue #2): The `PreCreate` hook queries PostgreSQL for the share token record and injects authoritative metadata (`token_label`, `token_id`, `created_by_admin`) into the upload's `.info` file via `HookResponse.ChangeFileInfo.MetaData`. This ensures the `.info` blob in Azure contains traceable, server-controlled metadata regardless of what the browser provides.

##### `PreCreate` Validation Table

| Check | Action on failure |
|-------|------------------|
| Share token present in upload metadata | Reject (HTTP 400) |
| Token is valid (§5.2.2) | Reject (HTTP 403) |
| `use_count < max_uses` | Reject (HTTP 403) |
| File MIME type in `allowed_file_types` | Reject (HTTP 415) |
| File size ≤ `max_file_size_bytes` | Reject (HTTP 413) |

#### 5.4.3 `PostFinish` Hook

Triggered in-process after tusd has received the final chunk and the upload is complete.

```go
func postFinishHook(ctx context.Context, event handler.HookEvent) (handler.HookResponse, error) {
    upload := event.Upload

    // 1. Record upload in DB
    uploadRecord, err := db.CreateUpload(ctx, db.CreateUploadParams{
        TusdID:      upload.ID,
        BlobKey:     upload.Storage["Key"],
        Filename:    upload.MetaData["filename"],
        MIMEType:    upload.MetaData["filetype"],
        SizeBytes:   upload.Size,
        Metadata:    upload.MetaData,
        ShareTokenID: tokenIDFromMeta(upload.MetaData),
    })
    if err != nil {
        return handler.HookResponse{}, err
    }

    // 2. Increment use_count atomically
    if err := db.IncrementUseCount(ctx, upload.MetaData["share_token"]); err != nil {
        return handler.HookResponse{}, err
    }

    // 3. Enqueue background job
    jobQueue <- Job{UploadID: uploadRecord.ID, BlobKey: upload.Storage["Key"]}

    return handler.HookResponse{}, nil
}
```

#### 5.4.4 Hook Payload (for reference)

The `handler.HookEvent` available to both hooks contains:

```go
type HookEvent struct {
    Upload   FileInfo   // ID, Size, MetaData, Storage (after finish)
    HTTPRequest HTTPRequest // Method, URI, RemoteAddr, Header
}
```

---

### 5.5 Azure Blob Storage via tusd

#### 5.5.1 Overview

tusd has **native, first-class Azure Blob Storage support** — no S3 compatibility layer is needed. Files stream directly to Azure during upload: only a small per-chunk temporary file is written to disk; the full file is never buffered locally.

#### 5.5.2 Configuration

The Go app configures the Azure store programmatically:

```go
azureConfig := &azurestore.AzureConfig{
    AccountName:   os.Getenv("AZURE_STORAGE_ACCOUNT"),
    // AccountKey left empty → uses DefaultAzureCredential (Workload Identity)
    ContainerName: "uploads",
    ObjectPrefix:  "uploads/",
    BlobAccessTier: "Cool",
    Endpoint:      os.Getenv("AZURE_STORAGE_ENDPOINT"), // empty in production
}
```

For local development with Azurite (see §12):

```bash
AZURE_STORAGE_ACCOUNT=devstoreaccount1
AZURE_STORAGE_KEY=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==
AZURE_STORAGE_ENDPOINT=http://azurite:10000/devstoreaccount1
```

#### 5.5.3 Authentication: AKS Workload Identity

In production, `AZURE_STORAGE_KEY` is left **empty**. The Azure SDK uses the `DefaultAzureCredential` chain, which automatically picks up **AKS Workload Identity** when the pod is annotated correctly (see §9.3). This eliminates the need for storage account keys or connection strings in Kubernetes Secrets.

#### 5.5.4 Storage Format

Each upload produces exactly **two blobs** in the configured container:

| Blob | Path | Contents |
|------|------|----------|
| File data | `uploads/<upload-id>` | Raw file bytes |
| Metadata | `uploads/<upload-id>.info` | JSON: filename, MIME type, size, custom metadata, server-injected token metadata |

#### 5.5.5 Incomplete Upload Expiry

Azure Blob Storage uncommitted block lists (from interrupted uploads) are automatically garbage-collected after **7 days** by an Azure Storage lifecycle policy. tusd's `WithExpiration` option also prunes incomplete uploads from its own tracking within 24 hours.

#### 5.5.6 Worker Access

The worker reads completed files directly from Azure Blob Storage using the blob key delivered in the `PostFinish` hook, stored in the `uploads` table. The worker uses the same `DefaultAzureCredential` / Workload Identity chain (or the Azurite connection string locally).

---

### 5.6 Background Processing

#### 5.6.1 Job Lifecycle

```
PENDING ──► RUNNING ──► SUCCEEDED
                   └──► FAILED ──► PENDING (retry)
                                └──► DEAD (max retries exceeded)
```

| State | Description |
|-------|-------------|
| `PENDING` | Job enqueued, waiting for a worker |
| `RUNNING` | Worker has picked up the job |
| `SUCCEEDED` | Processing completed successfully |
| `FAILED` | Processing failed; will be retried |
| `DEAD` | Max retries exceeded; manual intervention required |

#### 5.6.2 Worker Architecture

Two deployment options are supported, selectable via a command-line flag:

**Option A — In-process goroutine worker pool (default)**

Worker goroutines run inside the same Go binary as the HTTP server. A buffered channel acts as the job queue:

```go
// main.go
jobQueue := make(chan Job, 256)

// Start worker pool
for i := 0; i < cfg.WorkerConcurrency; i++ {
    go worker.Run(ctx, jobQueue, db, azureClient)
}
```

This is the simplest deployment model — a single container serves both HTTP traffic and processes jobs.

**Option B — Separate Worker Deployment**

The same Go binary is run with a `--worker` flag, which starts only the worker pool (consuming from a shared queue backed by PostgreSQL or Redis) and does not start the HTTP server:

```bash
# HTTP server
go run ./cmd/server

# Worker (separate process/pod)
go run ./cmd/server --worker
```

This allows independent scaling of the worker pool.

#### 5.6.3 Worker Behaviour

1. Worker dequeues a job from the in-process channel (Option A) or external queue (Option B).
2. Updates `jobs.state` to `RUNNING`, sets `started_at`.
3. Downloads the file from Azure Blob Storage using the blob key.
4. Runs the configured processor (see §5.6.4).
5. On success:
   - Stores the result (JSON) in `jobs.result`.
   - Sets `jobs.state = SUCCEEDED`, `completed_at`.
   - Sends an SSE event to any connected clients for `sse:<upload_id>`.
6. On failure:
   - Increments `jobs.attempt_count`.
   - If `attempt_count < max_retries`: re-enqueues with exponential backoff, sets `state = FAILED`.
   - If `attempt_count >= max_retries`: sets `state = DEAD`, sends error SSE event.

#### 5.6.4 Pluggable Processor Types

The processor type is configured per share token (or globally) and determines what the worker does with the file:

| Processor | Description |
|-----------|-------------|
| `noop` | No-op; immediately returns success (default / testing) |
| `virus_scan` | Scans file with ClamAV; result includes `clean: bool` |
| `metadata_extract` | Extracts file metadata (EXIF, PDF properties, etc.) |
| `custom_webhook` | POSTs the file (or blob URL) to a configurable external webhook |

#### 5.6.5 Retry Logic

| Parameter | Default |
|-----------|---------|
| Max retries | 3 |
| Backoff | Exponential: 30 s, 5 min, 30 min |
| Dead-letter | Jobs with `state = DEAD` visible in admin dashboard |

---

### 5.7 Result Communication

#### 5.7.1 SSE Endpoint

The upload page connects to the SSE endpoint immediately after the tus upload completes:

```
GET /events/<upload_id>?token=<share_token>
```

- The Go app validates the share token and the upload ID ownership before opening the SSE stream.
- The connection uses `Content-Type: text/event-stream` with `Cache-Control: no-cache`.
- For single-replica deployments, the Go app fans out SSE messages via an in-process pub/sub broker (a map of channels keyed by `upload_id`).
- For multi-replica deployments, Redis pub/sub (`sse:<upload_id>`) is used to relay messages across replicas.
- The connection is closed by the server after a `result` or `error` event is sent.

#### 5.7.2 SSE Event Types

| Event name | When sent | Data fields |
|------------|-----------|-------------|
| `queued` | Job enqueued (immediately after upload) | `upload_id`, `job_id` |
| `processing` | Worker picks up the job | `upload_id`, `job_id`, `started_at` |
| `result` | Job succeeded | `upload_id`, `job_id`, `result` (JSON), `completed_at` |
| `error` | Job failed (all retries exhausted) | `upload_id`, `job_id`, `error` (string), `attempt_count` |
| `heartbeat` | Every 15 s while waiting | — (keep-alive comment line) |

Example SSE stream:

```
data: {"event":"queued","upload_id":"abc123","job_id":"job_456"}

data: {"event":"processing","upload_id":"abc123","job_id":"job_456","started_at":"2026-03-10T12:00:05Z"}

data: {"event":"result","upload_id":"abc123","job_id":"job_456","result":{"clean":true},"completed_at":"2026-03-10T12:00:08Z"}
```

#### 5.7.3 Client Reconnection

- The browser's `EventSource` API reconnects automatically using the `Last-Event-ID` header.
- The Go app uses SSE `id:` fields so the client can resume from the last received event.
- If the client reconnects after the job has already completed, the Go app returns the stored result from the database immediately and closes the stream.

---

## 6. Data Models

### `admins`

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | UUID | PK, default `gen_random_uuid()` | Primary key |
| `email` | varchar(255) | UNIQUE NOT NULL | Login email |
| `password_hash` | varchar(255) | NOT NULL | bcrypt hash |
| `created_at` | timestamptz | NOT NULL, default `now()` | Creation timestamp |
| `last_login_at` | timestamptz | | Last successful login |
| `is_active` | boolean | NOT NULL, default `true` | Account enabled flag |

### `share_tokens`

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | UUID | PK, default `gen_random_uuid()` | Primary key |
| `token` | varchar(64) | UNIQUE NOT NULL | URL-safe random token |
| `label` | varchar(255) | | Human-readable label |
| `created_by` | UUID | FK → `admins.id` | Creating admin |
| `created_at` | timestamptz | NOT NULL, default `now()` | Creation timestamp |
| `expires_at` | timestamptz | | Expiry; `NULL` = never |
| `max_uses` | integer | | Max uploads; `NULL` = unlimited |
| `use_count` | integer | NOT NULL, default `0` | Completed uploads so far |
| `revoked_at` | timestamptz | | Revocation timestamp |
| `allowed_file_types` | text[] | | MIME type allowlist |
| `max_file_size_bytes` | bigint | | Per-file size limit |
| `metadata_schema` | jsonb | | JSON Schema for upload metadata |
| `notify_email` | varchar(255) | | Notification email |
| `processor_type` | varchar(64) | NOT NULL, default `'noop'` | Background processor to use |

### `uploads`

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | UUID | PK, default `gen_random_uuid()` | Primary key |
| `share_token_id` | UUID | FK → `share_tokens.id` NOT NULL | Associated token |
| `tusd_id` | varchar(255) | UNIQUE NOT NULL | tusd upload ID |
| `blob_key` | varchar(512) | NOT NULL | Azure Blob path (`uploads/<tusd-id>`) |
| `filename` | varchar(512) | NOT NULL | Original filename |
| `mime_type` | varchar(128) | | Detected / declared MIME type |
| `size_bytes` | bigint | | File size in bytes |
| `metadata` | jsonb | | Uploader-provided + server-injected metadata |
| `uploaded_at` | timestamptz | NOT NULL, default `now()` | Upload completion timestamp |
| `ip_address` | inet | | Uploader IP (for audit) |

### `jobs`

| Column | Type | Constraints | Description |
|--------|------|-------------|-------------|
| `id` | UUID | PK, default `gen_random_uuid()` | Primary key |
| `upload_id` | UUID | FK → `uploads.id` NOT NULL | Associated upload |
| `processor_type` | varchar(64) | NOT NULL | Processor used |
| `state` | varchar(32) | NOT NULL, default `'PENDING'` | `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `DEAD` |
| `attempt_count` | integer | NOT NULL, default `0` | Number of attempts so far |
| `max_retries` | integer | NOT NULL, default `3` | Maximum retry attempts |
| `enqueued_at` | timestamptz | NOT NULL, default `now()` | Time job was enqueued |
| `started_at` | timestamptz | | Time worker picked up the job |
| `completed_at` | timestamptz | | Time job finished (success or dead) |
| `result` | jsonb | | Processor output on success |
| `error_message` | text | | Error message on failure |

---

## 7. API Specification

### 7.1 Admin API

All endpoints require a valid admin session. Base path: `/admin/api/v1`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/auth/login` | Admin login; returns session cookie or JWT |
| `POST` | `/auth/logout` | Invalidate session |
| `GET` | `/tokens` | List share tokens (paginated) |
| `POST` | `/tokens` | Create a new share token |
| `GET` | `/tokens/:id` | Get share token details |
| `PATCH` | `/tokens/:id` | Update token (label, expiry, etc.) |
| `DELETE` | `/tokens/:id` | Revoke (soft-delete) a token |
| `GET` | `/uploads` | List uploads (paginated, filterable by token) |
| `GET` | `/uploads/:id` | Get upload details |
| `GET` | `/jobs` | List jobs (paginated, filterable by state) |
| `GET` | `/jobs/:id` | Get job details and result |
| `POST` | `/jobs/:id/retry` | Manually re-enqueue a dead job |

### 7.2 Public API

No authentication required beyond the share token in the URL.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/upload/<token>` | Serve the upload page HTML |
| `POST,PATCH,HEAD,DELETE,OPTIONS` | `/upload/tus/*` | Tus protocol endpoint (tusd embedded handler) |
| `GET` | `/events/<upload_id>?token=<share_token>` | SSE stream for job status updates |

> **Note:** There is no separate internal webhook endpoint. Hook logic (`PreCreate`, `PostFinish`) is implemented as in-process Go functions registered on the `tusd.Config` — no HTTP round-trip or shared secret is required.

---

## 8. Technology Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Backend framework | **Go** (`net/http` + `chi` or `echo` router) | App server, admin API, embedded tusd handler, SSE |
| Tus server | **tusd** (`github.com/tus/tusd/v2`) | Embedded as a Go library; no separate process |
| Storage | **Azure Blob Storage** | tusd native `azurestore` backend |
| Local storage emulator | **Azurite** | Drop-in Azure Blob Storage emulator for local dev |
| Task queue | **Go channels** + goroutine worker pool | In-process job queue; Redis optional for multi-replica |
| Database | **PostgreSQL 15+** | Persistent data store |
| Real-time | **Server-Sent Events (SSE)** | Job result push to browser |
| Admin frontend | **React** or **HTMX** | Admin dashboard UI |
| Upload frontend | **tus-js-client** | Tus resumable upload in the browser |
| Container orchestration | **Kubernetes (AKS)** | Production deployment |
| Identity | **AKS Workload Identity** | Passwordless Azure Blob Storage access |
| Auth (admin) | **bcrypt** + session or JWT | Admin login |
| DB migrations | **golang-migrate** or **goose** | Schema migrations |
| Token generation | **`crypto/rand`** (Go standard library) | Cryptographically secure token generation |

---

## 9. Kubernetes Deployment

### 9.1 Deployment Architecture

A single Go app Deployment serves both the HTTP API and the embedded tusd handler. Workers can run as goroutines within the same Deployment (Option A) or as a separate Deployment using the same binary with `--worker` (Option B):

```
┌──────────────────────────────────┐     ┌─────────────────┐
│  Go App Deployment               │     │  Worker         │
│  (N replicas)                    │     │  Deployment     │
│  - HTTP server                   │     │  (Option B only)│
│  - tusd.Handler (embedded)       │     │  (P replicas)   │
│  - Worker goroutines (Option A)  │     │                 │
└──────────────────┬───────────────┘     └────────┬────────┘
                   │                              │
                   └──────────────────────────────┘
                                  │
               ┌──────────────────┼──────────────────┐
               ▼                  ▼                   ▼
         ┌──────────┐      ┌──────────┐       ┌────────────────────┐
         │PostgreSQL│      │  Redis   │       │Azure Blob Storage  │
         │          │      │(optional)│       │                    │
         └──────────┘      └──────────┘       └────────────────────┘
```

### 9.2 Rationale for Single Deployment

| Concern | Benefit |
|---------|---------|
| Simpler operations | One container image to build, scan, and deploy |
| No inter-service latency | Hook logic runs in-process — no HTTP round-trip |
| No shared secret | Hooks are Go function calls — no `Hook-Secret` header needed |
| Smaller container image | Single Go binary, no Python/pip dependencies |
| Lower memory footprint | No separate tusd process; Go runtime is shared |
| Worker scaling | Workers can scale to 0 when idle (HPA / KEDA) via Option B |

Kubernetes Service topology:

| Service | Type | Consumers |
|---------|------|-----------|
| `app-service` | LoadBalancer / Ingress | External users |
| `redis-service` | ClusterIP | Go app, Worker (if Option B) |
| `postgres-service` | ClusterIP | Go app, Worker (if Option B) |

### 9.3 Go App Kubernetes Manifest Snippet

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: pooortal
spec:
  replicas: 2
  selector:
    matchLabels:
      app: pooortal
  template:
    metadata:
      labels:
        app: pooortal
      annotations:
        azure.workload.identity/use: "true"   # Enable Workload Identity on this pod
    spec:
      serviceAccountName: pooortal-workload-identity-sa  # Bound to Azure Managed Identity
      containers:
        - name: pooortal
          image: pooortal:latest
          args: []  # HTTP server mode (default)
          env:
            - name: AZURE_STORAGE_ACCOUNT
              valueFrom:
                secretKeyRef:
                  name: azure-storage-secret
                  key: account-name
            # AZURE_STORAGE_KEY is intentionally omitted — Workload Identity is used instead
            - name: DATABASE_URL
              valueFrom:
                secretKeyRef:
                  name: pooortal-secrets
                  key: database-url
          ports:
            - containerPort: 8080
          livenessProbe:
            httpGet:
              path: /health
              port: 8080
          readinessProbe:
            httpGet:
              path: /health
              port: 8080
---
apiVersion: v1
kind: Service
metadata:
  name: app-service
spec:
  type: LoadBalancer
  selector:
    app: pooortal
  ports:
    - port: 443
      targetPort: 8080
```

### 9.4 Workload Identity Setup

1. Create an Azure Managed Identity (User-Assigned).
2. Assign the `Storage Blob Data Contributor` role on the storage container.
3. Create a federated credential linking the AKS cluster's OIDC issuer to the Kubernetes ServiceAccount:
   ```bash
   az identity federated-credential create \
     --name pooortal-federated-credential \
     --identity-name pooortal-managed-identity \
     --resource-group <rg> \
     --issuer <aks-oidc-issuer-url> \
     --subject system:serviceaccount:default:pooortal-workload-identity-sa
   ```
4. Annotate the Kubernetes ServiceAccount:
   ```yaml
   apiVersion: v1
   kind: ServiceAccount
   metadata:
     name: pooortal-workload-identity-sa
     annotations:
       azure.workload.identity/client-id: <managed-identity-client-id>
   ```

---

## 10. Security Considerations

| Concern | Mitigation |
|---------|-----------|
| **Token entropy** | 256-bit random tokens via Go's `crypto/rand` — brute-force infeasible |
| **Token exposure** | Tokens are in URL path; advise against logging full URLs; consider short-lived tokens |
| **File type validation** | MIME type checked in `PreCreate` hook server-side; client-side check is UX-only |
| **File size limits** | Enforced server-side in `PreCreate` hook and by tusd's `MaxSize` config option |
| **CSRF** | Admin forms protected with CSRF tokens (Go middleware) |
| **XSS** | All user-supplied metadata HTML-escaped in templates; `Content-Security-Policy` header set |
| **IDOR** | SSE endpoint validates that the upload ID belongs to the share token in the request |
| **Workload Identity over keys** | `AZURE_STORAGE_KEY` left empty in production; no long-lived credentials in cluster |
| **Admin brute-force** | Account lockout after 10 failed attempts within 5 minutes |
| **Secrets management** | All secrets (DB password, Redis password) stored in Kubernetes Secrets |
| **TLS** | All external traffic served over HTTPS; internal cluster traffic optionally mTLS via service mesh |
| **Malware** | Worker can run `virus_scan` processor before making results available |

---

## 11. Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| Maximum single file size | 50 GB |
| Concurrent uploads | 100 simultaneous active Tus uploads per Go app replica |
| Upload throughput | Limited by Azure Blob Storage ingress bandwidth |
| API response time (p95) | < 200 ms for non-upload endpoints |
| SSE result delivery latency | < 5 s from job completion to client notification |
| Availability | 99.9% (excluding planned maintenance) |
| Tus upload resumability | Incomplete uploads retained for 24 hours |
| Database connection pool | Minimum 5, maximum 20 connections per Go app replica |
| Worker concurrency | Configurable; default 4 concurrent jobs per worker goroutine pool |
| Log retention | 30 days in cluster logging solution |

---

## 12. Local Development

### 12.1 Docker Compose Setup

A `docker-compose.yml` at the repository root provides a complete local environment with all dependencies:

```yaml
version: "3.9"

services:
  app:
    build: .
    ports:
      - "8080:8080"
    environment:
      DATABASE_URL: postgresql://pooortal:pooortal@postgres:5432/pooortal
      AZURE_STORAGE_ACCOUNT: devstoreaccount1
      AZURE_STORAGE_KEY: "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
      AZURE_STORAGE_ENDPOINT: http://azurite:10000/devstoreaccount1
      WORKER_CONCURRENCY: "4"
    depends_on:
      - postgres
      - azurite

  azurite:
    image: mcr.microsoft.com/azure-storage/azurite:3.31.0
    ports:
      - "10000:10000"
    command: azurite-blob --blobHost 0.0.0.0

  postgres:
    image: postgres:15
    environment:
      POSTGRES_USER: pooortal
      POSTGRES_PASSWORD: pooortal
      POSTGRES_DB: pooortal
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data

volumes:
  postgres_data:
```

> **Note:** Redis is optional in local development. For single-replica local dev, the in-process SSE broker handles fan-out without Redis.

### 12.2 Running Without Docker

```bash
# Start dependencies only
docker compose up -d postgres azurite

# Run the HTTP server + embedded worker (Option A)
go run ./cmd/server

# Or run server and worker separately (Option B)
go run ./cmd/server &
go run ./cmd/server --worker
```

### 12.3 Azurite Connection String

The well-known Azurite development connection string:

```
DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://azurite:10000/devstoreaccount1;
```

### 12.4 Initial Setup

```bash
# Start all services
docker compose up -d

# Run database migrations
go run ./cmd/server migrate

# Create the first admin account
go run ./cmd/server create-admin --email admin@example.com --password changeme

# Open the admin UI
open http://localhost:8080/admin
```

---

## 13. Open Questions

1. **Processing types:** What are the concrete processor types needed for v1? Is `virus_scan` a hard requirement, or can `noop` + `custom_webhook` cover all initial use cases?

2. **Multi-file uploads:** Should a single share token allow uploading multiple files in one session? If so, does the background job operate on the batch or on each file independently?

3. **Result page:** Should the result page (SSE output) be a simple JSON display, or does each processor type need its own rendered result template? Can results contain downloadable output files?

4. **Admin SSO:** Is SSO / OAuth2 (e.g. Azure AD / Entra ID) required for the admin interface in v1, or is username/password sufficient initially?

5. **File retention policy:** How long should completed upload blobs be retained in Azure Blob Storage after successful processing? Should the worker delete the source blob? Should there be a configurable retention period per token?

6. **Audit log:** Is a structured audit log (admin actions, upload events, job state transitions) required for compliance purposes? Should it be stored in PostgreSQL or shipped to an external SIEM?

7. **Upload page branding:** Should the upload page support custom branding (logo, colours, copy) per share token, or is a global theme sufficient?

8. **Rate limiting:** Should rate limiting be applied per IP address on the upload page and Tus endpoint? If so, what are the acceptable limits, and should Redis be used for rate limit counters?

9. **Metadata schema enforcement:** When `metadata_schema` is set on a share token, should validation happen client-side (JSON Schema in the browser), server-side (in `PreCreate` hook), or both?

10. **Notification webhooks:** Beyond `notify_email`, should the system support outbound webhooks (HTTP POST) to notify external systems of upload completion or job results?
