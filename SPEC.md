# pooortal — Technical Specification

**Version:** 1.0.0
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
   - [5.4 Flask ↔ tusd Proxy & Hook Architecture](#54-flask--tusd-proxy--hook-architecture)
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
          tus-js-client uploads file chunks ──► Flask proxy ──► tusd ──► Azure Blob Storage
                          │
                          ▼
          tusd post-finish hook ──► Flask records upload, enqueues job
                          │
                          ▼
          Worker processes file (reads from Azure Blob Storage)
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
        │                    Flask (App Server)                │
        │  /admin/*      Admin API                             │
        │  /upload/<token>  Serve upload page                  │
        │  /upload/tus/*    Reverse proxy → tusd               │
        │  /events/<token>  SSE endpoint                       │
        │  /internal/hooks/tusd  Internal webhook (tusd hooks) │
        └────────┬────────────────────────┬─────────────────── ┘
                 │ HTTP (internal)         │ Enqueue job
                 ▼                         ▼
   ┌─────────────────────┐      ┌──────────────────────┐
   │       tusd           │      │   Redis (Task Queue) │
   │  (Tus server)        │      └──────────┬───────────┘
   │  ClusterIP only      │                 │
   └──────────┬──────────┘                 ▼
              │ Streams chunks      ┌──────────────────┐
              ▼                     │  Worker Process  │
   ┌─────────────────────┐          │  (Celery / RQ)   │
   │  Azure Blob Storage │◄─────────┘                  │
   │  (tusd native)      │  Reads completed file        │
   └─────────────────────┘                             │
                                                        │ Result
                                                        ▼
                                             ┌──────────────────┐
                                             │   PostgreSQL DB   │
                                             │  (jobs, uploads,  │
                                             │   share_tokens)   │
                                             └──────────────────┘
```

### Component Overview

| Component | Technology | Responsibility |
|-----------|-----------|----------------|
| Flask app | Python / Flask | Admin API, upload page, Tus proxy, SSE, tusd hooks |
| tusd | Go (tusd) | Tus resumable upload protocol, Azure Blob Storage streaming |
| Worker | Python (Celery/RQ) | Background file processing |
| PostgreSQL | PostgreSQL 15+ | Persistent data (tokens, uploads, jobs, admins) |
| Redis | Redis 7+ | Task queue, SSE pub/sub channel |
| Azure Blob Storage | Azure / Azurite | Durable file storage |

---

## 4. User Roles

| Role | Description | Authentication |
|------|-------------|----------------|
| **Admin** | Creates and manages shareable URLs, views upload/job status, configures constraints | Username + password session (or SSO) |
| **Uploader** | External user who follows a shareable URL and uploads file(s) | None — share token in URL grants access |
| **Worker** | Internal background process; consumes jobs from queue and processes uploads | Internal only (no HTTP auth) |

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

On creation, a cryptographically random **share token** is generated (32 bytes, URL-safe base64) and a full URL is returned:

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
| Generation | `secrets.token_urlsafe(32)` (Python) |
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

- `use_count` is incremented atomically (database-level) in the `post-finish` tusd hook handler, only after a file has been fully and successfully uploaded.
- The `pre-create` hook checks `use_count < max_uses` to reject new uploads when the token is exhausted.

---

### 5.3 File Upload Form / Tus Integration

#### 5.3.1 Upload Page UI

The upload page is served by Flask at `/upload/<token>` and renders:

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
    share_token: shareToken,     // validated server-side
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

Server-side enforcement occurs in the tusd `pre-create` hook (see §5.4.2).

#### 5.3.4 Resumability

- tus-js-client stores the upload URL in `localStorage` keyed by file fingerprint.
- If the user refreshes the page or loses connectivity, resuming is automatic.
- Incomplete uploads on the server expire after 24 hours (tusd `--expiration` flag).

---

### 5.4 Flask ↔ tusd Proxy & Hook Architecture

#### 5.4.1 Reverse Proxy Route

Flask exposes a reverse proxy at `/upload/tus/<path:remainder>` that:

1. Validates the share token found in the request metadata or `Authorization` header.
2. Forwards the request verbatim (method, headers, body) to tusd at `http://tusd-service:8080/files/<remainder>`.
3. Streams the response back to the client.
4. Forwards all Tus-relevant response headers (`Upload-Offset`, `Tus-Resumable`, `Location`, etc.).

Supported HTTP methods: `POST`, `PATCH`, `HEAD`, `DELETE`, `OPTIONS`.

```
Client ──► Flask /upload/tus/* ──► tusd http://tusd-service:8080/files/*
```

tusd is bound to a Kubernetes ClusterIP Service and is **not** publicly exposed.

#### 5.4.2 tusd HTTP Hooks

tusd is started with `--hooks-http=http://flask-service/internal/hooks/tusd`.

All hook requests from tusd include the header:

```
Hook-Secret: <TUSD_HOOK_SECRET>
```

Flask validates this header on every request to `/internal/hooks/tusd` and returns HTTP 401 if the secret is missing or incorrect.

##### `pre-create` Hook

Triggered before tusd creates a new upload resource.

Flask performs:

| Check | Action on failure |
|-------|------------------|
| Share token present in upload metadata | Reject (HTTP 400) |
| Token is valid (§5.2.2) | Reject (HTTP 403) |
| `use_count < max_uses` | Reject (HTTP 403) |
| File MIME type in `allowed_file_types` | Reject (HTTP 415) |
| File size ≤ `max_file_size_bytes` | Reject (HTTP 413) |

Returning a non-2xx response causes tusd to abort the upload and return an error to the client.

##### `post-finish` Hook

Triggered after tusd has received the final chunk and the upload is complete.

Flask performs:

1. Validates the `Hook-Secret` header.
2. Reads the upload metadata from the hook payload (blob key, filename, MIME type, share token, user metadata).
3. Records the upload in the `uploads` table.
4. Increments `share_tokens.use_count` atomically.
5. Enqueues a background job in Redis with the upload ID and blob key.
6. Returns HTTP 200.

#### 5.4.3 Hook Payload Example

```json
{
  "Type": "post-finish",
  "Event": {
    "Upload": {
      "ID": "a1b2c3d4e5f6...",
      "Size": 104857600,
      "MetaData": {
        "filename": "report.pdf",
        "filetype": "application/pdf",
        "share_token": "abc123...",
        "custom_field": "value"
      },
      "Storage": {
        "Type": "azureblob",
        "Container": "uploads",
        "Key": "uploads/a1b2c3d4e5f6..."
      }
    }
  }
}
```

---

### 5.5 Azure Blob Storage via tusd

#### 5.5.1 Overview

tusd has **native, first-class Azure Blob Storage support** — no S3 compatibility layer is needed. Files stream directly to Azure during upload: only a small per-chunk temporary file is written to disk; the full file is never buffered locally.

#### 5.5.2 Configuration

tusd is started with the following flags and environment variables:

```bash
# Environment variables
AZURE_STORAGE_ACCOUNT=<storage-account-name>
AZURE_STORAGE_KEY=               # leave empty to use Entra ID / Workload Identity

# CLI flags
tusd \
  -azure-storage=uploads \        # container name
  -azure-object-prefix=uploads/ \ # namespace blobs under uploads/ prefix
  -azure-blob-access-tier=cool \  # cost-optimised storage tier
  -hooks-http=http://flask-service/internal/hooks/tusd \
  -hooks-http-forward-headers=Hook-Secret \
  -expiration=24h
```

For local development with Azurite (see §12):

```bash
AZURE_STORAGE_ACCOUNT=devstoreaccount1
AZURE_STORAGE_KEY=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==
tusd -azure-endpoint=http://azurite:10000/devstoreaccount1 ...
```

#### 5.5.3 Authentication: AKS Workload Identity

In production, `AZURE_STORAGE_KEY` is left **empty**. tusd uses the `DefaultAzureCredential` chain (from the Azure SDK), which automatically picks up **AKS Workload Identity** when the pod is annotated correctly (see §9.3). This eliminates the need for storage account keys or connection strings in Kubernetes Secrets.

#### 5.5.4 Storage Format

Each upload produces exactly **two blobs** in the configured container:

| Blob | Path | Contents |
|------|------|----------|
| File data | `uploads/<upload-id>` | Raw file bytes |
| Metadata | `uploads/<upload-id>.info` | JSON: filename, MIME type, size, custom metadata |

#### 5.5.5 Incomplete Upload Expiry

Azure Blob Storage uncommitted block lists (from interrupted uploads) are automatically garbage-collected after **7 days** by an Azure Storage lifecycle policy. tusd's `--expiration=24h` flag also prunes incomplete uploads from its own tracking within 24 hours.

#### 5.5.6 Worker Access

The worker reads completed files directly from Azure Blob Storage using the blob key delivered in the `post-finish` hook payload, stored in the `uploads` table. The worker uses the same `DefaultAzureCredential` / Workload Identity chain (or the Azurite connection string locally).

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

#### 5.6.2 Worker Behaviour

1. Worker dequeues a job from Redis.
2. Updates `jobs.state` to `RUNNING`, sets `started_at`.
3. Downloads the file from Azure Blob Storage using the blob key.
4. Runs the configured processor (see §5.6.3).
5. On success:
   - Stores the result (JSON) in `jobs.result`.
   - Sets `jobs.state = SUCCEEDED`, `completed_at`.
   - Publishes an SSE event to Redis pub/sub channel `sse:<upload_id>`.
6. On failure:
   - Increments `jobs.attempt_count`.
   - If `attempt_count < max_retries`: re-enqueues with exponential backoff, sets `state = FAILED`.
   - If `attempt_count >= max_retries`: sets `state = DEAD`, publishes error SSE event.

#### 5.6.3 Pluggable Processor Types

The processor type is configured per share token (or globally) and determines what the worker does with the file:

| Processor | Description |
|-----------|-------------|
| `noop` | No-op; immediately returns success (default / testing) |
| `virus_scan` | Scans file with ClamAV; result includes `clean: bool` |
| `metadata_extract` | Extracts file metadata (EXIF, PDF properties, etc.) |
| `custom_webhook` | POSTs the file (or blob URL) to a configurable external webhook |

#### 5.6.4 Retry Logic

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

- Flask validates the share token and the upload ID ownership before opening the SSE stream.
- The connection uses `Content-Type: text/event-stream` with `Cache-Control: no-cache`.
- Flask subscribes to the Redis pub/sub channel `sse:<upload_id>` and forwards messages to the client.
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
- Flask uses SSE `id:` fields so the client can resume from the last received event.
- If the client reconnects after the job has already completed, Flask returns the stored result from the database immediately and closes the stream.

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
| `metadata` | jsonb | | Uploader-provided metadata |
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
| `POST,PATCH,HEAD,DELETE,OPTIONS` | `/upload/tus/*` | Tus protocol reverse proxy to tusd |
| `GET` | `/events/<upload_id>?token=<share_token>` | SSE stream for job status updates |

### 7.3 Internal / Webhook Endpoints

These endpoints are not publicly routable (protected by Kubernetes NetworkPolicy and/or shared secret).

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/internal/hooks/tusd` | Receives `pre-create` and `post-finish` hooks from tusd |

Request headers required:

```
Hook-Secret: <TUSD_HOOK_SECRET>
```

Hook type is determined by the `Hook-Name` header sent by tusd (`pre-create`, `post-finish`).

---

## 8. Technology Stack

| Layer | Technology | Notes |
|-------|-----------|-------|
| Backend framework | **Flask** (Python 3.12+) | App server, admin API, reverse proxy, SSE |
| Tus server | **tusd** (Go) | Official Tus reference implementation |
| Storage | **Azure Blob Storage** | tusd native `-azure-storage` backend |
| Local storage emulator | **Azurite** | Drop-in Azure Blob Storage emulator for local dev |
| Task queue | **Redis** + **Celery** or **RQ** | Job queue and SSE pub/sub |
| Database | **PostgreSQL 15+** | Persistent data store |
| Real-time | **Server-Sent Events (SSE)** | Job result push to browser |
| Admin frontend | **React** or **HTMX** | Admin dashboard UI |
| Upload frontend | **tus-js-client** | Tus resumable upload in the browser |
| Container orchestration | **Kubernetes (AKS)** | Production deployment |
| Identity | **AKS Workload Identity** | Passwordless Azure Blob Storage access |
| Auth (admin) | **bcrypt** + session or JWT | Admin login |
| ORM | **SQLAlchemy** + **Alembic** | DB access and migrations |

---

## 9. Kubernetes Deployment

### 9.1 Deployment Architecture

Three separate Kubernetes Deployments are used — one per logical component:

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Flask          │     │  tusd           │     │  Worker         │
│  Deployment     │◄───►│  Deployment     │     │  Deployment     │
│  (N replicas)   │     │  (M replicas)   │     │  (P replicas)   │
└────────┬────────┘     └────────┬────────┘     └────────┬────────┘
         │                       │                        │
         └───────────────────────┴────────────────────────┘
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                   ▼
        ┌──────────┐      ┌──────────┐       ┌────────────────────┐
        │PostgreSQL│      │  Redis   │       │Azure Blob Storage  │
        └──────────┘      └──────────┘       └────────────────────┘
```

### 9.2 Rationale for Multi-Deployment

| Concern | Benefit |
|---------|---------|
| Independent scaling | Flask and tusd can be scaled separately based on load |
| Zero-downtime deploys | Rolling Flask updates do NOT kill in-progress uploads (tusd is separate) |
| Resource isolation | Each component has its own CPU/memory limits |
| Health checks | Each component has independent liveness/readiness probes |
| Log isolation | Each component's logs are separately queryable |
| Worker scaling | Workers can scale to 0 when idle (HPA / KEDA) |

Kubernetes Service topology:

| Service | Type | Consumers |
|---------|------|-----------|
| `flask-service` | LoadBalancer / Ingress | External users, tusd (for hooks) |
| `tusd-service` | ClusterIP | Flask (for Tus proxy) |
| `redis-service` | ClusterIP | Flask, Worker |
| `postgres-service` | ClusterIP | Flask, Worker |

### 9.3 tusd Kubernetes Manifest Snippet

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: tusd
spec:
  replicas: 2
  selector:
    matchLabels:
      app: tusd
  template:
    metadata:
      labels:
        app: tusd
      annotations:
        azure.workload.identity/use: "true"   # Enable Workload Identity on this pod
    spec:
      serviceAccountName: tusd-workload-identity-sa  # Bound to Azure Managed Identity
      containers:
        - name: tusd
          image: tusproject/tusd:v2.3.0
          args:
            - -azure-storage=uploads
            - -azure-object-prefix=uploads/
            - -azure-blob-access-tier=cool
            - -hooks-http=http://flask-service/internal/hooks/tusd
            - -hooks-http-forward-headers=Hook-Secret
            - -expiration=24h
            - -behind-proxy
          env:
            - name: AZURE_STORAGE_ACCOUNT
              valueFrom:
                secretKeyRef:
                  name: azure-storage-secret
                  key: account-name
            # AZURE_STORAGE_KEY is intentionally omitted — Workload Identity is used instead
            - name: TUSD_HOOK_SECRET
              valueFrom:
                secretKeyRef:
                  name: tusd-hook-secret
                  key: secret
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
  name: tusd-service
spec:
  type: ClusterIP   # Not publicly exposed
  selector:
    app: tusd
  ports:
    - port: 8080
      targetPort: 8080
```

### 9.4 Workload Identity Setup

1. Create an Azure Managed Identity (User-Assigned).
2. Assign the `Storage Blob Data Contributor` role on the storage container.
3. Create a federated credential linking the AKS cluster's OIDC issuer to the Kubernetes ServiceAccount:
   ```bash
   az identity federated-credential create \
     --name tusd-federated-credential \
     --identity-name tusd-managed-identity \
     --resource-group <rg> \
     --issuer <aks-oidc-issuer-url> \
     --subject system:serviceaccount:default:tusd-workload-identity-sa
   ```
4. Annotate the Kubernetes ServiceAccount:
   ```yaml
   apiVersion: v1
   kind: ServiceAccount
   metadata:
     name: tusd-workload-identity-sa
     annotations:
       azure.workload.identity/client-id: <managed-identity-client-id>
   ```

---

## 10. Security Considerations

| Concern | Mitigation |
|---------|-----------|
| **Token entropy** | 256-bit random tokens via `secrets.token_urlsafe(32)` — brute-force infeasible |
| **Token exposure** | Tokens are in URL path; advise against logging full URLs; consider short-lived tokens |
| **File type validation** | MIME type checked in `pre-create` hook server-side; client-side check is UX-only |
| **File size limits** | Enforced server-side in `pre-create` hook and by tusd's `-max-size` flag |
| **CSRF** | Admin forms protected with CSRF tokens (Flask-WTF or equivalent) |
| **XSS** | All user-supplied metadata HTML-escaped in templates; `Content-Security-Policy` header set |
| **IDOR** | SSE endpoint validates that the upload ID belongs to the share token in the request |
| **Hook endpoint protection** | `/internal/hooks/tusd` requires `Hook-Secret` header; not publicly routable (NetworkPolicy) |
| **Workload Identity over keys** | `AZURE_STORAGE_KEY` left empty in production; no long-lived credentials in cluster |
| **Admin brute-force** | Account lockout after 10 failed attempts within 5 minutes |
| **Secrets management** | All secrets (DB password, Redis password, hook secret) stored in Kubernetes Secrets |
| **TLS** | All external traffic served over HTTPS; internal cluster traffic optionally mTLS via service mesh |
| **Malware** | Worker can run `virus_scan` processor before making results available |

---

## 11. Non-Functional Requirements

| Requirement | Target |
|-------------|--------|
| Maximum single file size | 50 GB |
| Concurrent uploads | 100 simultaneous active Tus uploads per tusd replica |
| Upload throughput | Limited by Azure Blob Storage ingress bandwidth |
| API response time (p95) | < 200 ms for non-upload endpoints |
| SSE result delivery latency | < 5 s from job completion to client notification |
| Availability | 99.9% (excluding planned maintenance) |
| Tus upload resumability | Incomplete uploads retained for 24 hours |
| Database connection pool | Minimum 5, maximum 20 connections per Flask replica |
| Worker concurrency | Configurable; default 4 concurrent jobs per worker replica |
| Log retention | 30 days in cluster logging solution |

---

## 12. Local Development

### 12.1 Docker Compose Setup

A `docker-compose.yml` at the repository root provides a complete local environment with all dependencies:

```yaml
version: "3.9"

services:
  flask:
    build: .
    ports:
      - "5000:5000"
    environment:
      FLASK_ENV: development
      DATABASE_URL: postgresql://pooortal:pooortal@postgres:5432/pooortal
      REDIS_URL: redis://redis:6379/0
      TUSD_URL: http://tusd:8080
      TUSD_HOOK_SECRET: dev-secret
    depends_on:
      - postgres
      - redis
      - tusd

  tusd:
    image: tusproject/tusd:v2.3.0
    command: >
      -azure-storage=uploads
      -azure-object-prefix=uploads/
      -hooks-http=http://flask:5000/internal/hooks/tusd
      -hooks-http-forward-headers=Hook-Secret
      -expiration=24h
      -behind-proxy
      -azure-endpoint=http://azurite:10000/devstoreaccount1
    environment:
      AZURE_STORAGE_ACCOUNT: devstoreaccount1
      AZURE_STORAGE_KEY: "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
      TUSD_HOOK_SECRET: dev-secret
    ports:
      - "8080:8080"
    depends_on:
      - azurite

  worker:
    build: .
    command: celery -A pooortal.celery worker --loglevel=info
    environment:
      DATABASE_URL: postgresql://pooortal:pooortal@postgres:5432/pooortal
      REDIS_URL: redis://redis:6379/0
      AZURE_STORAGE_ACCOUNT: devstoreaccount1
      AZURE_STORAGE_KEY: "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
      AZURE_STORAGE_ENDPOINT: http://azurite:10000/devstoreaccount1
    depends_on:
      - postgres
      - redis
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

  redis:
    image: redis:7
    ports:
      - "6379:6379"

volumes:
  postgres_data:
```

### 12.2 Azurite Connection String

The well-known Azurite development connection string:

```
DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://azurite:10000/devstoreaccount1;
```

### 12.3 Initial Setup

```bash
# Start all services
docker compose up -d

# Run database migrations
docker compose exec flask flask db upgrade

# Create the first admin account
docker compose exec flask flask create-admin --email admin@example.com --password changeme

# Open the admin UI
open http://localhost:5000/admin
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

8. **Rate limiting:** Should rate limiting be applied per IP address on the upload page and Tus proxy? If so, what are the acceptable limits, and should Redis be used for rate limit counters?

9. **Metadata schema enforcement:** When `metadata_schema` is set on a share token, should validation happen client-side (JSON Schema in the browser), server-side (in `pre-create` hook), or both?

10. **Notification webhooks:** Beyond `notify_email`, should the system support outbound webhooks (HTTP POST) to notify external systems of upload completion or job results?
