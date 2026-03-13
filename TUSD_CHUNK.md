# CHUNK

why only one patch request on upload?

Good observation. In your HAR trace the entire 2 GB was indeed sent in **a single PATCH request** — that's the default behavior of tus-js-client when no `chunkSize` option is set.

## How it works

The tus protocol flow is:

1. **POST** `/upload/` — creates the upload resource, sends metadata + `Upload-Length`. Returns `Location` header with the upload URL. Body is empty.
2. **PATCH** `/upload/<id>` — sends the actual file data with `Upload-Offset: 0` and `Content-Type: application/offset+octet-stream`.

By default, tus-js-client streams the **entire file in one PATCH**. The browser reads the `File` blob and sends it as a single HTTP request body. The `Content-Length: 2147483648` in your trace confirms all 2 GB went in one go.

The **progress events** you see in the UI come from the browser's `XMLHttpRequest.upload.onprogress` — the browser fires progress callbacks as it transmits the request body over the TCP connection, even though it's a single HTTP request.

## Enabling actual chunked uploads

To split the upload into multiple PATCH requests, set `chunkSize`:

```js
const upload = new tus.Upload(file, {
  endpoint: TUSD_ENDPOINT,
  chunkSize: 50 * 1024 * 1024, // 50 MB per PATCH request
  retryDelays: [0, 3000, 5000, 10000],
  // ...
});
```

With `chunkSize: 50MB` on a 2 GB file you'd see ~40 sequential PATCH requests, each with the appropriate `Upload-Offset`. This is more resilient — if a chunk fails, only that chunk needs retrying instead of restarting the full 2 GB transfer.

**Trade-off:** Without `chunkSize`, you get maximum throughput (no per-chunk overhead). With it, you get better resumability on unreliable connections.

---

wouldnt it be better for server side resource requirements to split up the upload?

Not really, for the reason that **tusd streams incoming data directly to the storage backend** — it doesn't buffer the entire request body in memory. Whether you send 1 × 2 GB or 40 × 50 MB, tusd's memory usage stays roughly constant (a small read buffer, typically ~64 KB–1 MB).

**Where chunking does help:**

| Concern | Single PATCH | Chunked |
|---|---|---|
| **Client resumability** | Must restart full 2 GB on failure | Retry only the failed chunk |
| **Reverse proxy timeouts** | nginx/LB may timeout on long transfers | Each chunk completes faster |
| **Azure block size limits** | Azure has max 4 GB per block, 50k blocks per blob | Smaller blocks, more flexibility |
| **Client memory** (browser) | Browser holds File reference, streams fine | Same |

**Where chunking hurts:**

- More HTTP round-trips (connection overhead, latency per chunk)
- More hook invocations if `pre-finish` or `post-receive` hooks are configured per chunk
- Slightly more complex state tracking

**Bottom line:** For a production deployment behind nginx (as in your K8s diagram), chunking is recommended — not for tusd's memory, but because **nginx has a default 60s proxy timeout** (`proxy_read_timeout`). A 2 GB single PATCH could easily exceed that. Setting `chunkSize` to something like 50–100 MB keeps each request well within timeout limits.

If you want to add chunking to your upload page, I can update the tus-js-client config.

---

is chunk szie only configurable from client? or can we also require this from serverside?



**Chunk size is client-side only** — tusd has no server-side setting to enforce or require a specific chunk size. The server accepts whatever the client sends in each PATCH request.

tusd does have **`-max-size`** to limit the total upload size, but nothing like `-max-chunk-size` to reject individual PATCH requests that are too large.

**If you need to enforce it server-side**, your options are:

1. **nginx `client_max_body_size`** — set it to e.g. `100m` in the `/upload/` location block. This rejects any single request body larger than 100 MB, effectively forcing clients to chunk. This is the most practical approach for your K8s/nginx setup.

2. **tusd `pre-receive` hook** — this hook fires during data transfer and includes the current offset/size. You could reject transfers mid-stream, but this is clunky — the data is already being sent.

The nginx approach is the cleanest: set `client_max_body_size 100m;` on the tusd location and tus-js-client will automatically retry with the `retryDelays` config after getting a `413 Request Entity Too Large`, though it's better to just **set `chunkSize` on the client to match**.