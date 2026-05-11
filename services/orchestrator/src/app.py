import os
import sys
import json
import grpc
import logging
import uuid
import time

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
from order_queue import order_queue_pb2 as oq_pb2
from order_queue import order_queue_pb2_grpc as oq_grpc


from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient


OTEL_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://observability:4318")

resource = Resource.create({"service.name": os.getenv("OTEL_SERVICE_NAME", "orchestrator")})

trace.set_tracer_provider(TracerProvider(resource=resource))
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTEL_ENDPOINT}/v1/traces"))
)

metrics.set_meter_provider(
    MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{OTEL_ENDPOINT}/v1/metrics")
        )],
    )
)

RequestsInstrumentor().instrument()
GrpcInstrumentorClient().instrument()

# Flask app setup 
app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)
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


VC_COMPONENTS = ("transaction_verification", "fraud_detection", "suggestions")


class BackendServiceError(RuntimeError):
    """Raised when a backend service call fails at transport/service level."""


def new_vector_clock():
    return {name: 0 for name in VC_COMPONENTS}

def merge_clock(local_clock: dict, incoming_clock: dict) -> dict:
    merged = dict(local_clock or {})
    for name in VC_COMPONENTS:
        merged[name] = max(int((local_clock or {}).get(name, 0)), int((incoming_clock or {}).get(name, 0)))
    return merged

def init_all_services(order_id: str, order_dict: dict, vc: dict, request_id: str):
    payload = json.dumps(order_dict)

    def init_tv():
        try:
            with grpc.insecure_channel("transaction_verification:50052") as channel:
                stub = tv_grpc.TransactionVerificationServiceStub(channel)
                req = tv_pb2.OrderInitializationRequest(order_id=order_id, order_json=payload, vector_clock=vc)
                return stub.InitializeOrder(req, timeout=3)
        except grpc.RpcError as e:
            raise BackendServiceError(
                f"TV init call failed: {e.code().name if hasattr(e, 'code') else 'UNKNOWN'}"
            ) from e

    def init_fd():
        try:
            with grpc.insecure_channel("fraud_detection:50051") as channel:
                stub = fd_grpc.FraudDetectionServiceStub(channel)
                req = fd_pb2.OrderInitializationRequest(order_id=order_id, order_json=payload, vector_clock=vc)
                return stub.InitializeOrder(req, timeout=3)
        except grpc.RpcError as e:
            raise BackendServiceError(
                f"FD init call failed: {e.code().name if hasattr(e, 'code') else 'UNKNOWN'}"
            ) from e

    def init_sg():
        try:
            with grpc.insecure_channel("suggestions:50053") as channel:
                stub = sg_grpc.SuggestionsServiceStub(channel)
                req = sg_pb2.OrderInitializationRequest(order_id=order_id, order_json=payload, vector_clock=vc)
                return stub.InitializeOrder(req, timeout=3)
        except grpc.RpcError as e:
            raise BackendServiceError(
                f"SG init call failed: {e.code().name if hasattr(e, 'code') else 'UNKNOWN'}"
            ) from e

    log.info("[%s] InitOrder: order_id=%s vc=%s", request_id, order_id, vc)

    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="init") as ex:
        f_tv = ex.submit(init_tv)
        f_fd = ex.submit(init_fd)
        f_sg = ex.submit(init_sg)

        r_tv = f_tv.result()
        r_fd = f_fd.result()
        r_sg = f_sg.result()

    if not r_tv.accepted:
        raise RuntimeError(f"TV init rejected: {r_tv.reason}")
    if not r_fd.accepted:
        raise RuntimeError(f"FD init rejected: {r_fd.reason}")
    if not r_sg.accepted:
        raise RuntimeError(f"SG init rejected: {r_sg.reason}")

    merged = merge_clock(vc, dict(r_tv.vector_clock))
    merged = merge_clock(merged, dict(r_fd.vector_clock))
    merged = merge_clock(merged, dict(r_sg.vector_clock))
    log.info("[%s] InitOrder done: merged_vc=%s", request_id, merged)
    return merged

def tv_event(order_id: str, method_name: str, vc: dict, request_id: str):
    try:
        with grpc.insecure_channel("transaction_verification:50052") as channel:
            stub = tv_grpc.TransactionVerificationServiceStub(channel)
            req = tv_pb2.OrderEventRequest(order_id=order_id, vector_clock=vc)
            method = getattr(stub, method_name)
            resp = method(req, timeout=3)
            new_vc = merge_clock(vc, dict(resp.vector_clock))
            log.info("[%s] TV.%s => success=%s reason=%s event=%s vc=%s",
                     request_id, method_name, resp.success, resp.reason, resp.event_name, new_vc)
            return resp.success, resp.reason, resp.event_name, new_vc
    except grpc.RpcError as e:
        log.error(
            "[%s] TV.%s gRPC error: code=%s details=%s",
            request_id,
            method_name,
            e.code() if hasattr(e, "code") else "UNKNOWN",
            e.details() if hasattr(e, "details") else str(e),
        )
        raise BackendServiceError(
            f"transaction_verification.{method_name} unavailable"
        ) from e

def fd_event(order_id: str, method_name: str, vc: dict, request_id: str):
    try:
        with grpc.insecure_channel("fraud_detection:50051") as channel:
            stub = fd_grpc.FraudDetectionServiceStub(channel)
            req = fd_pb2.OrderEventRequest(order_id=order_id, vector_clock=vc)
            method = getattr(stub, method_name)
            resp = method(req, timeout=3)
            new_vc = merge_clock(vc, dict(resp.vector_clock))
            log.info("[%s] FD.%s => success=%s reason=%s event=%s vc=%s",
                     request_id, method_name, resp.success, resp.reason, resp.event_name, new_vc)
            return resp.success, resp.reason, resp.event_name, new_vc
    except grpc.RpcError as e:
        log.error(
            "[%s] FD.%s gRPC error: code=%s details=%s",
            request_id,
            method_name,
            e.code() if hasattr(e, "code") else "UNKNOWN",
            e.details() if hasattr(e, "details") else str(e),
        )
        raise BackendServiceError(
            f"fraud_detection.{method_name} unavailable"
        ) from e

def sg_event_generate(order_id: str, vc: dict, request_id: str):
    try:
        with grpc.insecure_channel("suggestions:50053") as channel:
            stub = sg_grpc.SuggestionsServiceStub(channel)
            req = sg_pb2.OrderEventRequest(order_id=order_id, vector_clock=vc)
            resp = stub.GenerateSuggestions(req, timeout=3)
            new_vc = merge_clock(vc, dict(resp.vector_clock))
            log.info("[%s] SG.GenerateSuggestions => success=%s reason=%s event=%s vc=%s books=%s",
                     request_id, resp.success, resp.reason, resp.event_name, new_vc, len(resp.books))
            books = [{"bookId": b.book_id, "title": b.title, "author": b.author} for b in resp.books]
            return resp.success, resp.reason, resp.event_name, new_vc, books
    except grpc.RpcError as e:
        log.error(
            "[%s] SG.GenerateSuggestions gRPC error: code=%s details=%s",
            request_id,
            e.code() if hasattr(e, "code") else "UNKNOWN",
            e.details() if hasattr(e, "details") else str(e),
        )
        raise BackendServiceError("suggestions.GenerateSuggestions unavailable") from e

def clear_all_services(order_id: str, final_vc: dict, request_id: str):
    def clear_tv():
        with grpc.insecure_channel("transaction_verification:50052") as channel:
            stub = tv_grpc.TransactionVerificationServiceStub(channel)
            req = tv_pb2.OrderClearRequest(order_id=order_id, final_vector_clock=final_vc)
            return stub.ClearOrder(req, timeout=3)

    def clear_fd():
        with grpc.insecure_channel("fraud_detection:50051") as channel:
            stub = fd_grpc.FraudDetectionServiceStub(channel)
            req = fd_pb2.OrderClearRequest(order_id=order_id, final_vector_clock=final_vc)
            return stub.ClearOrder(req, timeout=3)

    def clear_sg():
        with grpc.insecure_channel("suggestions:50053") as channel:
            stub = sg_grpc.SuggestionsServiceStub(channel)
            req = sg_pb2.OrderClearRequest(order_id=order_id, final_vector_clock=final_vc)
            return stub.ClearOrder(req, timeout=3)

    futures = {}
    try:
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="clear") as ex:
            futures[ex.submit(clear_tv)] = "transaction_verification"
            futures[ex.submit(clear_fd)] = "fraud_detection"
            futures[ex.submit(clear_sg)] = "suggestions"

        for future, service_name in futures.items():
            try:
                response = future.result()
                log.info(
                    "[%s] ClearOrder %s => cleared=%s reason=%s",
                    request_id,
                    service_name,
                    getattr(response, "cleared", "?"),
                    getattr(response, "reason", ""),
                )
            except Exception as service_error:
                log.warning(
                    "[%s] ClearOrder to service %s failed: %s",
                    request_id,
                    service_name,
                    service_error,
                )

        log.info("[%s] ClearOrder broadcast sent order_id=%s final_vc=%s", request_id, order_id, final_vc)
    except Exception as e:
        log.warning("[%s] ClearOrder broadcast submission failed: %s", request_id, e)


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

    order_id = uuid.uuid4().hex
    vc = new_vector_clock()

    try:
        vc = init_all_services(order_id, request_data, vc, request_id)
    except Exception as e:
        log.error("[%s] InitOrder failed: %s", request_id, e)
        clear_all_services(order_id, vc, request_id)
        error_message = "Failed to initialize backend services"
        reason = str(e).strip()
        if reason:
            error_message = f"{error_message}: {reason}"
        return jsonify({"error": {"code": "SERVICE_UNAVAILABLE", "message": error_message}}), 503

    try:
        # a || b in parallel
        def run_a():
            return tv_event(order_id, "VerifyItemsNonEmpty", vc, request_id)

        def run_b():
            return tv_event(order_id, "VerifyUserData", vc, request_id)

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="events") as ex:
            fa = ex.submit(run_a)
            fb = ex.submit(run_b)
            a_success, a_reason, _, vc_a = fa.result()
            b_success, b_reason, _, vc_b = fb.result()

        vc = merge_clock(vc, vc_a)
        vc = merge_clock(vc, vc_b)

        if not a_success:
            clear_all_services(order_id, vc, request_id)
            return jsonify({"orderId": order_id, "status": "Order Rejected", "suggestedBooks": []}), 200

        if not b_success:
            clear_all_services(order_id, vc, request_id)
            return jsonify({"orderId": order_id, "status": "Order Rejected", "suggestedBooks": []}), 200

        # c after a
        c_success, c_reason, _, vc = tv_event(order_id, "VerifyCreditCard", vc, request_id)
        if not c_success:
            clear_all_services(order_id, vc, request_id)
            return jsonify({"orderId": order_id, "status": "Order Rejected", "suggestedBooks": []}), 200

        # d after b
        d_success, d_reason, _, vc = fd_event(order_id, "CheckUserFraud", vc, request_id)
        if not d_success:
            clear_all_services(order_id, vc, request_id)
            return jsonify({"orderId": order_id, "status": "Order Rejected", "suggestedBooks": []}), 200

        # e after (c and d)
        e_success, e_reason, _, vc = fd_event(order_id, "CheckCardFraud", vc, request_id)
        if not e_success:
            clear_all_services(order_id, vc, request_id)
            return jsonify({"orderId": order_id, "status": "Order Rejected", "suggestedBooks": []}), 200

        # f after e
        f_success, f_reason, _, vc, books = sg_event_generate(order_id, vc, request_id)
        if not f_success:
            clear_all_services(order_id, vc, request_id)
            return jsonify({"orderId": order_id, "status": "Order Rejected", "suggestedBooks": []}), 200

        # Enqueue for execution if all validation passed
        try:
            with grpc.insecure_channel("order_queue:50054") as channel:
                stub = oq_grpc.OrderQueueServiceStub(channel)
                order_pb = oq_pb2.Order(
                    order_id=order_id,
                    order_json=json.dumps(request_data),
                    timestamp=int(time.time() * 1000)
                )
                enqueue_req = oq_pb2.EnqueueRequest(order=order_pb)
                enqueue_resp = stub.Enqueue(enqueue_req, timeout=3)
                
                if not enqueue_resp.success:
                    log.error("[%s] Enqueue failed: %s", request_id, enqueue_resp.reason)
                    clear_all_services(order_id, vc, request_id)
                    return jsonify({
                        "orderId": order_id,
                        "status": "Order Approved But Queue Failed",
                        "suggestedBooks": books
                    }), 500
                
                log.info("[%s] Order enqueued successfully: %s", request_id, enqueue_resp.queue_position)
        
        except grpc.RpcError as e:
            log.error("[%s] Enqueue gRPC error: code=%s details=%s", request_id, e.code(), e.details())
            clear_all_services(order_id, vc, request_id)
            return jsonify({
                "orderId": order_id,
                "status": "Service Unavailable",
                "suggestedBooks": []
            }), 503
        
        except Exception as e:
            log.error("[%s] Unexpected enqueue error: %s", request_id, e)
            clear_all_services(order_id, vc, request_id)
            return jsonify({
                "orderId": order_id,
                "status": "Service Error",
                "suggestedBooks": []
            }), 500

        clear_all_services(order_id, vc, request_id)
        return jsonify({"orderId": order_id, "status": "Order Approved", "suggestedBooks": books}), 200
    except BackendServiceError as e:
        log.error("[%s] Event flow failed due to backend error: %s", request_id, e)
        clear_all_services(order_id, vc, request_id)
        return jsonify({
            "error": {
                "code": "SERVICE_UNAVAILABLE",
                "message": f"Backend service unavailable: {e}",
            }
        }), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0")
