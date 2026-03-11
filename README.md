# pooortal

Minimal Python Flask application that processes a [tusd](https://github.com/tus/tusd)
`pre-create` hook to validate a token in the upload metadata, authorizing or
denying the upload before it begins.

## How it works

[tusd](https://github.com/tus/tusd) supports lifecycle hooks that are called at
various points during an upload. The `pre-create` hook is invoked **before** an
upload is created, making it the ideal place to authenticate the request.

When a client starts an upload it passes a `token` value in the
`Upload-Metadata` header. tusd forwards the full upload context to this
Flask application as a JSON POST request. The application checks whether the
token is in the set of valid tokens and returns:

* **HTTP 200** – upload is allowed.
* **HTTP 401** – upload is denied (invalid or missing token).

## Setup

```bash
pip install -r requirements.txt
```

## Running

```bash
# Optional: supply your own comma-separated list of valid tokens
export VALID_TOKENS="my-secret,another-secret"

python app.py
# Server starts on http://127.0.0.1:5000
```

## Configuring tusd

Start tusd and point the `pre-create` hook at the Flask server:

```bash
tusd -hooks-http http://127.0.0.1:5000/hooks/pre-create
```

## Client example

Use any tus-compatible client and pass the token as upload metadata:

```bash
# Using tusc (https://github.com/eventials/go-tus)
tusc -metadata "token=my-secret" /path/to/file http://localhost:1080/files/
```

Or via `curl` (raw tus protocol):

```bash
curl -X POST http://localhost:1080/files/ \
  -H "Tus-Resumable: 1.0.0" \
  -H "Upload-Length: $(wc -c < /path/to/file)" \
  -H "Upload-Metadata: token $(echo -n 'my-secret' | base64)"
```

## Hook payload

tusd sends a JSON body like the following to the hook endpoint:

```json
{
  "Upload": {
    "ID": "",
    "Size": 1024,
    "Offset": 0,
    "MetaData": {
      "token": "my-secret"
    },
    "Storage": null
  }
}
```
