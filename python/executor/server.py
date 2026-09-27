from flask import Flask, request, jsonify
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# This file is <repo>/python/executor/server.py, so the Python package root is
# <repo>/python and the repository root - where .env lives - is one level above.
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REPO_ROOT = os.path.dirname(_PKG_ROOT)

# Load repo-root .env before anything reads the environment, so secrets never
# have to be re-exported in every terminal. A real env var still wins.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_REPO_ROOT, ".env"), override=False, encoding="utf-8-sig")
except ImportError:
    pass


def repo_path(env_name: str, default_relative: str) -> str:
    """Resolve a path setting against the REPO ROOT, not the current directory.

    The executor is started from <repo>/python, so a relative path like
    ./python/credentials.json would otherwise resolve to python/python/... and
    silently point at nothing.
    """
    value = os.getenv(env_name, "").strip()
    candidate = value or default_relative
    if os.path.isabs(candidate):
        return candidate
    return os.path.normpath(os.path.join(_REPO_ROOT, candidate))


from executor.executor import WorkflowExecutor, ExecutionResult
from shared.events import WorkflowSpec

app = Flask(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format='{"time":"%(asctime)s","level":"%(levelname)s","message":"%(message)s"}')
logger = logging.getLogger("workflowos.executor.api")

config = {
    'gmail_credentials': repo_path('GMAIL_CREDENTIALS', 'python/credentials.json'),
    'gmail_token': repo_path('GMAIL_TOKEN', 'python/token.json'),
    'slack_webhook': os.getenv('SLACK_WEBHOOK_URL', ''),
    'crm_url': os.getenv('CRM_URL', 'http://localhost:8080'),
    'download_dir': repo_path('DOWNLOAD_DIR', 'python/downloads'),
}

executor = WorkflowExecutor(config)


@app.route('/execute', methods=['POST'])
def execute_workflow():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Expected a JSON object"}), 400
    workflow_id = data.get('workflow_id')
    spec_data = data.get('spec')

    if not workflow_id or not isinstance(spec_data, dict):
        return jsonify({"error": "Missing workflow spec"}), 400

    spec = WorkflowSpec.from_dict(spec_data)

    result = executor.execute(spec)
    logger.info("execution workflow_id=%s success=%s steps=%d", workflow_id, result.success, len(result.steps))
    status_code = 200 if result.success else 500
    return jsonify({
        "workflow_id": workflow_id,
        "success": result.success,
        "message": result.message,
        "outputs": result.outputs,
        "steps": result.steps,
    }), status_code


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})


@app.route('/readyz', methods=['GET'])
def ready():
    status = executor.readiness()
    return jsonify(status), 200 if status["ready"] else 503


if __name__ == '__main__':
    port = int(os.getenv('PYTHON_EXECUTOR_PORT', '8082'))
    app.run(host='0.0.0.0', port=port, debug=False)
