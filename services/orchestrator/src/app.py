import os
import sys
import json
import grpc
import logging
import uuid

from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify
from flask_cors import CORS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("orchestrator")

# Import gRPC generated stubs 
FILE = __file__ if '__file__' in globals() else os.getenv("PYTHONFILE", "")
pb_root = os.path.abspath(os.path.join(FILE, "../../../../utils/pb"))
sys.path.insert(0, pb_root)

from fraud_detection import fraud_detection_pb2 as fd_pb2
from fraud_detection import fraud_detection_pb2_grpc as fd_grpc
from transaction_verification import transaction_verification_pb2 as tv_pb2
from transaction_verification import transaction_verification_pb2_grpc as tv_grpc
from suggestions import suggestions_pb2 as sg_pb2
from suggestions import suggestions_pb2_grpc as sg_grpc

# Flask app setup 
app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})


def mask_sensitive_data(data):
    """
    Mask sensitive data
    """
    if not isinstance(data, dict):
        return data
    
    masked = data.copy()

    if 'creditCard' in masked and isinstance(masked['creditCard'], dict):
        cc = masked['creditCard'].copy()
        if 'number' in cc and cc['number']:
            cc['number'] = '****' + str(cc['number'])[-4:] if len(str(cc['number'])) >= 4 else '****'
        if 'cvv' in cc:
            cc['cvv'] = '***'
        masked['creditCard'] = cc
    
    return masked


def summarize_order(data):
    """
    Build a log-safe order summary.
    """
    if not isinstance(data, dict):
        return {"has_payload": False}

    items = data.get("items") or []
    total_quantity = 0
    for item in items:
        try:
            total_quantity += int(item.get("quantity", 0))
        except (TypeError, ValueError, AttributeError):
            continue

    user = data.get("user") or {}
    credit_card = data.get("creditCard") or {}
    card_number = str(credit_card.get("number", ""))

    return {
        "item_count": len(items),
        "total_quantity": total_quantity,
        "has_user_name": bool(user.get("name")),
        "has_user_contact": bool(user.get("contact")),
        "card_suffix": card_number[-4:] if card_number else None,
    }


def get_request_id():
    return request.headers.get("X-Request-ID") or uuid.uuid4().hex[:8]


def call_fraud_detection(order_dict):
    """
    Calls fraud_detection gRPC service and returns (fraud_detected: bool, reason: str)
    """
    with grpc.insecure_channel("fraud_detection:50051") as channel:
        stub = fd_grpc.FraudDetectionServiceStub(channel)
        req = fd_pb2.OrderRequest(order_json=json.dumps(order_dict))
        resp = stub.CheckFraud(req, timeout=3)
        return resp.fraud_detected, resp.reason


def call_transaction_verification(order_dict):
    """
    Calls transaction_verification gRPC service and returns (is_valid: bool, reason: str)
    """
    with grpc.insecure_channel("transaction_verification:50052") as channel:
        stub = tv_grpc.TransactionVerificationServiceStub(channel)
        req = tv_pb2.TransactionRequest(order_json=json.dumps(order_dict))
        resp = stub.VerifyTransaction(req, timeout=3)
        return resp.is_valid, resp.reason


def call_suggestions(order_dict):
    """
    Calls suggestions gRPC service and returns list of book suggestions
    """
    with grpc.insecure_channel("suggestions:50053") as channel:
        stub = sg_grpc.SuggestionsServiceStub(channel)
        req = sg_pb2.SuggestionsRequest(order_json=json.dumps(order_dict))
        resp = stub.GetSuggestions(req, timeout=3)
        return [
            {"bookId": book.book_id, "title": book.title, "author": book.author}
            for book in resp.books
        ]


def run_fraud_detection(order_dict, request_id):
    log.info("[%s] Calling fraud_detection", request_id)
    try:
        fraud_detected, fraud_reason = call_fraud_detection(order_dict)
        result = {"detected": fraud_detected, "reason": fraud_reason, "error": None}
        log.info(
            "[%s] fraud_detection completed: detected=%s reason=%s",
            request_id,
            fraud_detected,
            fraud_reason,
        )
        return result
    except grpc.RpcError as e:
        log.error(
            "[%s] fraud_detection gRPC error: code=%s details=%s",
            request_id,
            e.code(),
            e.details(),
        )
        return {
            "detected": True,
            "reason": "Fraud detection service unavailable",
            "error": "SERVICE_UNAVAILABLE",
        }
    except Exception as e:
        log.exception("[%s] fraud_detection unexpected error: %s", request_id, e)
        return {
            "detected": True,
            "reason": "Fraud detection service error",
            "error": "SERVICE_ERROR",
        }


def run_transaction_verification(order_dict, request_id):
    log.info("[%s] Calling transaction_verification", request_id)
    try:
        is_valid, reason = call_transaction_verification(order_dict)
        result = {"valid": is_valid, "reason": reason, "error": None}
        log.info(
            "[%s] transaction_verification completed: valid=%s reason=%s",
            request_id,
            is_valid,
            reason,
        )
        return result
    except grpc.RpcError as e:
        log.error(
            "[%s] transaction_verification gRPC error: code=%s details=%s",
            request_id,
            e.code(),
            e.details(),
        )
        return {
            "valid": False,
            "reason": "Transaction verification service unavailable",
            "error": "SERVICE_UNAVAILABLE",
        }
    except Exception as e:
        log.exception("[%s] transaction_verification unexpected error: %s", request_id, e)
        return {
            "valid": False,
            "reason": "Transaction verification service error",
            "error": "SERVICE_ERROR",
        }


def run_suggestions(order_dict, request_id):
    log.info("[%s] Calling suggestions", request_id)
    try:
        books = call_suggestions(order_dict)
        result = {"books": books, "error": None}
        log.info("[%s] suggestions completed: books=%s", request_id, len(books))
        return result
    except grpc.RpcError as e:
        log.error(
            "[%s] suggestions gRPC error: code=%s details=%s",
            request_id,
            e.code(),
            e.details(),
        )
        return {"books": [], "error": "SERVICE_UNAVAILABLE"}
    except Exception as e:
        log.exception("[%s] suggestions unexpected error: %s", request_id, e)
        return {"books": [], "error": "SERVICE_ERROR"}


@app.route("/", methods=["GET"])
def index():
    # simple health check endpoint
    return "Orchestrator is running", 200


@app.route("/checkout", methods=["POST"])
def checkout():
    request_id = get_request_id()
    log.info("[%s] Received checkout request - Content-Type: %s", request_id, request.content_type)
    
    # Parse JSON safely
    request_data = request.get_json(silent=True)
    if request_data is None and request.data:
        try:
            request_data = json.loads(request.data.decode("utf-8"))
        except Exception as e:
            log.error("[%s] Failed to parse JSON: %s", request_id, e)
            request_data = None

    log.info(
        "[%s] Parsed request (masked): %s | summary=%s",
        request_id,
        mask_sensitive_data(request_data),
        summarize_order(request_data),
    )
    
    if request_data is None:
        return jsonify({"error": {"code": "INVALID_JSON", "message": "Invalid or missing JSON body"}}), 400

    # Validate required fields according to API contract
    items = request_data.get("items")
    if not isinstance(items, list) or len(items) == 0:
        return jsonify({"error": {"code": "INVALID_ITEMS", "message": "items must be a non-empty list"}}), 400
    
    # Validate user information
    user = request_data.get("user")
    if not user or not isinstance(user, dict):
        return jsonify({"error": {"code": "MISSING_USER", "message": "user information is required"}}), 400
    
    # Validate credit card information
    credit_card = request_data.get("creditCard")
    if not credit_card or not isinstance(credit_card, dict):
        return jsonify({"error": {"code": "MISSING_CREDIT_CARD", "message": "creditCard information is required"}}), 400

    log.info("[%s] Request validation passed - %s items", request_id, len(items))

    log.info("[%s] Dispatching backend requests via ThreadPoolExecutor", request_id)
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="checkout-worker") as executor:
        futures = {
            'fraud': executor.submit(run_fraud_detection, request_data, request_id),
            'transaction': executor.submit(run_transaction_verification, request_data, request_id),
            'suggestions': executor.submit(run_suggestions, request_data, request_id),
        }
        results = {name: future.result() for name, future in futures.items()}

    log.info("[%s] All backend calls completed", request_id)
    
    # Extract results from shared dict
    fraud_data = results.get('fraud', {})
    fraud_detected = fraud_data.get('detected', True)
    fraud_reason = fraud_data.get('reason', 'Unknown')
    fraud_error = fraud_data.get('error')
    
    transaction_data = results.get('transaction', {})
    transaction_valid = transaction_data.get('valid', False)
    transaction_reason = transaction_data.get('reason', 'Unknown')
    transaction_error = transaction_data.get('error')
    
    suggestions_data = results.get('suggestions', {})
    suggested_books = suggestions_data.get('books', [])
    suggestions_error = suggestions_data.get('error')
    
    log.info(
        "[%s] Results - Fraud: %s (%s), Transaction: %s (%s), Suggestions: %s books",
        request_id,
        fraud_detected,
        fraud_reason,
        transaction_valid,
        transaction_reason,
        len(suggested_books),
    )

    # Check if any critical service failed
    if fraud_error or transaction_error:
        error_details = []
        if fraud_error:
            error_details.append("fraud_detection")
        if transaction_error:
            error_details.append("transaction_verification")
        
        log.error("[%s] Critical services unavailable: %s", request_id, ', '.join(error_details))
        return jsonify({
            "error": {
                "code": "SERVICE_UNAVAILABLE",
                "message": f"One or more backend services are unavailable: {', '.join(error_details)}"
            }
        }), 503

    # Consolidate results: reject if fraud detected OR transaction invalid
    approved = (not fraud_detected) and transaction_valid
    if not approved:
        suggested_books = []
        log.info(
            "[%s] Order rejected - Fraud: %s, Valid: %s",
            request_id,
            fraud_detected,
            transaction_valid,
        )
    else:
        log.info("[%s] Order approved", request_id)

    order_status_response = {
        "orderId": "12345",
        "status": "Order Approved" if approved else "Order Rejected",
        "suggestedBooks": suggested_books
    }

    return jsonify(order_status_response), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0")
