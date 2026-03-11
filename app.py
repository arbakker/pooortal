import os

from flask import Flask, jsonify, request

app = Flask(__name__)

# A set of valid tokens. In a real application, these would be stored
# in a database or checked against an external service.
VALID_TOKENS = set(os.environ.get("VALID_TOKENS", "secret-token").split(","))


@app.route("/hooks/pre-create", methods=["POST"])
def pre_create_hook():
    """
    Handle the tusd pre-create hook.

    tusd sends a POST request to this endpoint before creating an upload.
    The request body contains JSON with upload information, including any
    metadata passed by the client.

    The client is expected to include a ``token`` field in the upload
    metadata, for example::

        tus-client upload --metadata "token=<value>" <file>

    Or via the Upload-Metadata HTTP header::

        Upload-Metadata: token <base64-encoded-value>

    Returns HTTP 200 to allow the upload, or HTTP 401 to deny it.
    """
    body = request.get_json(silent=True)
    if body is None:
        return jsonify({"error": "Invalid request body"}), 400

    metadata = body.get("Upload", {}).get("MetaData", {})
    token = metadata.get("token")

    if not token or token not in VALID_TOKENS:
        return jsonify({"error": "Unauthorized: invalid or missing token"}), 401

    return jsonify({"message": "Upload authorized"}), 200


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug)
