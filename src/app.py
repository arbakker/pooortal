import json
import os

from flask import Flask, abort, jsonify, render_template, request
from flask_cors import CORS

app = Flask(__name__)
# Enable CORS for all routes and allow credentials for testing
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=True)

# Only allow uploads whose target URI starts with this prefix.
ALLOWED_UPLOAD_PATH = "/upload/"

# URL of the tusd server's upload endpoint, used by the browser frontend.
TUSD_URL = os.environ.get("TUSD_URL", "http://localhost:1080/upload/")

# A set of valid tokens. In a real application, these would be stored
# in a database or checked against an external service.
VALID_TOKENS = set(os.environ.get("VALID_TOKENS", "secret-token").split(","))


@app.route("/")
def landing():
    """Serve landing page with instructions."""
    return render_template("landing.html")


@app.route("/<token>")
def index(token):
    """Serve the browser-based upload frontend."""
    if token not in VALID_TOKENS:
        abort(403)
    return render_template("index.html", token=token, tusd_url=TUSD_URL)


@app.route("/hooks/pre-create", methods=["POST"])
def pre_create_hook():
    """
    Handle the tusd pre-create hook.

    tusd sends a POST request to this endpoint before creating an upload.
    The request body contains JSON with upload information wrapped in an 'Event'
    object, including the original HTTP request details and any metadata passed
    by the client.

    Two checks are performed:

    1. **Path check** – the upload is only allowed when the client's original
       request URI starts with ``/upload/``.  The URI is taken from
       ``Event.HTTPRequest.URI`` in the hook payload.  Uploads targeting any
       other path are rejected with HTTP 403.

    2. **Token check** – the client must include a ``token`` field in the
       upload metadata.  The token is validated against the ``VALID_TOKENS``
       environment variable (comma-separated).  An invalid or missing token is
       rejected with HTTP 403.

    Returns HTTP 200 to allow the upload.

    Rejections are also returned as HTTP 200 to the hook, but with
    ``RejectUpload: true`` and the desired status code/body in
    ``HTTPResponse``, so tusd forwards the correct status to the client.
    """
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Invalid request body"}), 400

    # Enforce that the upload is targeting /upload/
    uri = body.get("Event", {}).get("HTTPRequest", {}).get("URI", "")
    if not uri.startswith(ALLOWED_UPLOAD_PATH):
        return _reject(403, f"Forbidden: uploads are only accepted on {ALLOWED_UPLOAD_PATH}")

    metadata = body.get("Event", {}).get("Upload", {}).get("MetaData", {})
    token = metadata.get("token")

    if not token or token not in VALID_TOKENS:
        return _reject(403, "Forbidden: invalid or missing token")

    return jsonify({}), 200


def _reject(status_code, message):
    """Return a tusd hook response that rejects the upload with a custom status code."""
    return jsonify({
        "RejectUpload": True,
        "HTTPResponse": {
            "StatusCode": status_code,
            "Body": json.dumps({"error": message}),
            "Header": {
                "Content-Type": "application/json",
            },
        },
    }), 200


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug)
