from flask import Flask, request, jsonify
import os
import hmac
import hashlib
import time
import logging
from runner import run_mock

app = Flask(__name__)
logger = logging.getLogger("thin_slice.slack")

SLACK_SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET', '')


def verify_slack_request(body: bytes, timestamp: str, signature: str) -> bool:
    try:
        if not timestamp or not signature:
            return False
        # Reject requests older than 5 minutes
        if abs(time.time() - int(timestamp)) > 60 * 5:
            return False
        basestring = f"v0:{timestamp}:".encode() + body
        computed = "v0=" + hmac.new(SLACK_SIGNING_SECRET.encode(), basestring, hashlib.sha256).hexdigest()
        return hmac.compare_digest(computed, signature)
    except Exception:
        return False


@app.route('/slack/events', methods=['POST'])
def slack_event():
    if not SLACK_SIGNING_SECRET:
        logger.error("SLACK_SIGNING_SECRET not configured")
        return jsonify({'error': 'slack signing secret not configured'}), 500

    timestamp = request.headers.get('X-Slack-Request-Timestamp', '')
    signature = request.headers.get('X-Slack-Signature', '')
    body = request.get_data()
    if not verify_slack_request(body, timestamp, signature):
        logger.warning("Invalid Slack signature or stale request")
        return jsonify({'error': 'invalid signature'}), 401

    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({'error': 'invalid json payload'}), 400

    # url_verification handshake
    if payload.get('type') == 'url_verification':
        return jsonify({'challenge': payload.get('challenge')}), 200

    event = payload.get('event', {}) or {}
    text = event.get('text') or payload.get('text') or payload.get('message')
    repo_path = payload.get('repo_path') or payload.get('team_id') or 'sandbox/tails'

    if not isinstance(text, str) or not text.strip():
        return jsonify({'error': 'no text in payload'}), 400

    try:
        result = run_mock(text, repo_path)
    except Exception as exc:
        logger.exception("Error running pipeline")
        return jsonify({'error': 'processing error', 'detail': str(exc)}), 500

    # Sanitize response: don't return raw code blocks to Slack
    resp = {
        'policy_clearance': result.get('policy_clearance'),
        'projected_cost_usd': result.get('projected_token_cost_usd'),
        'recommendation': result.get('recommendation_notes'),
        'generated_files': list(result.get('generated_code_blocks', {}).keys()),
        'pull_request_url': result.get('pull_request_url')
    }
    return jsonify(resp), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5001))
    app.run(host='0.0.0.0', port=port)
