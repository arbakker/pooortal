import os

from flask import Flask, jsonify, request

app = Flask(__name__)

# Only allow uploads whose target URI starts with this prefix.
ALLOWED_UPLOAD_PATH = "/upload/"

# A set of valid tokens. In a real application, these would be stored
# in a database or checked against an external service.
VALID_TOKENS = set(os.environ.get("VALID_TOKENS", "secret-token").split(","))


@app.route("/hooks/pre-create", methods=["POST"])
def pre_create_hook():
    """
    Handle the tusd pre-create hook.

    tusd sends a POST request to this endpoint before creating an upload.
    The request body contains JSON with upload information, including the
    original HTTP request details and any metadata passed by the client.

    Two checks are performed:

    1. **Path check** – the upload is only allowed when the client's original
       request URI starts with ``/upload/``.  The URI is taken from
       ``HTTPRequest.URI`` in the hook payload.  Uploads targeting any other
       path are rejected with HTTP 403.

    2. **Token check** – the client must include a ``token`` field in the
       upload metadata (``Upload-Metadata`` header).  The token is validated
       against the ``VALID_TOKENS`` environment variable (comma-separated).
       An invalid or missing token is rejected with HTTP 401.

    Returns HTTP 200 to allow the upload.
    """
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Invalid request body"}), 400

    # Enforce that the upload is targeting /upload/
    uri = body.get("HTTPRequest", {}).get("URI", "")
    if not uri.startswith(ALLOWED_UPLOAD_PATH):
        return jsonify({"error": f"Forbidden: uploads are only accepted on {ALLOWED_UPLOAD_PATH}"}), 403

    metadata = body.get("Upload", {}).get("MetaData", {})
    token = metadata.get("token")

    if not token or token not in VALID_TOKENS:
        return jsonify({"error": "Unauthorized: invalid or missing token"}), 401

    return jsonify({"message": "Upload authorized"}), 200


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug)
