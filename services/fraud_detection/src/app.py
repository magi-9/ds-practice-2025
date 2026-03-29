import sys
import os
import json
import re
import copy
import logging
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Optional
from uuid import uuid4

# This set of lines are needed to import the gRPC stubs.
# The path of the stubs is relative to the current file, or absolute inside the container.
# Change these lines only if strictly needed.
FILE = __file__ if '__file__' in globals() else os.getenv("PYTHONFILE", "")
pb_root = os.path.abspath(os.path.join(FILE, "../../../../utils/pb"))
sys.path.insert(0, pb_root)

import fraud_detection.fraud_detection_pb2 as fd_pb2  # pyright: ignore[reportMissingImports]
import fraud_detection.fraud_detection_pb2_grpc as fd_grpc  # pyright: ignore[reportMissingImports]

import grpc
from concurrent import futures

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("fraud_detection")

SERVICE_COMPONENTS = (
    "transaction_verification",
    "fraud_detection",
    "suggestions",
)


@dataclass
class EventResult:
    success: bool
    reason: str = "OK"
    event_name: str = ""
    vector_clock: dict[str, int] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderState:
    order: dict[str, Any]
    vector_clock: dict[str, int]
    event_status: dict[str, bool] = field(default_factory=dict)
    failure_reason: Optional[str] = None


def summarize_order(order):
    items = order.get("items") or []
    total_quantity = 0
    for item in items:
        try:
            total_quantity += int(item.get("quantity", 0))
        except (TypeError, ValueError, AttributeError):
            continue

    user = order.get("user") or {}
    credit = order.get("creditCard") or {}
    card_number = str(credit.get("number", ""))

    return {
        "item_count": len(items),
        "total_quantity": total_quantity,
        "has_user_name": bool(user.get("name")),
        "has_user_contact": bool(user.get("contact")),
        "card_suffix": card_number[-4:] if card_number else None,
    }


class FraudDetectionService(fd_grpc.FraudDetectionServiceServicer):
    def __init__(self):
        self._orders: dict[str, OrderState] = {}
        self._lock = RLock()
        self._service_name = "fraud_detection"

    def _new_vector_clock(self):
        return {component: 0 for component in SERVICE_COMPONENTS}

    def _merge_clock(self, local_clock, incoming_clock):
        if not incoming_clock:
            return

        for component in SERVICE_COMPONENTS:
            try:
                incoming_value = int(incoming_clock.get(component, 0))
            except (TypeError, ValueError, AttributeError):
                incoming_value = 0
            local_clock[component] = max(local_clock.get(component, 0), incoming_value)

    def _clock_lte(self, left_clock, right_clock):
        for component in SERVICE_COMPONENTS:
            if int(left_clock.get(component, 0)) > int(right_clock.get(component, 0)):
                return False
        return True

    def _log_event(self, order_id, event_name, vector_clock, success, reason):
        log.info(
            "order_id=%s event=%s vc=%s success=%s reason=%s",
            order_id,
            event_name,
            vector_clock,
            success,
            reason,
        )

    def cache_order(self, order_id, order, incoming_clock=None):
        with self._lock:
            state = self._orders.get(order_id)
            if state is None:
                state = OrderState(
                    order=copy.deepcopy(order),
                    vector_clock=self._new_vector_clock(),
                )
                self._orders[order_id] = state
            else:
                state.order = copy.deepcopy(order)

            self._merge_clock(state.vector_clock, incoming_clock)
            clock_snapshot = copy.deepcopy(state.vector_clock)

        log.info("Cached fraud order_id=%s vc=%s", order_id, clock_snapshot)
        return clock_snapshot

    def clear_order(self, order_id, final_vector_clock=None):
        with self._lock:
            state = self._orders.get(order_id)
            if state is None:
                return False

            if final_vector_clock and not self._clock_lte(state.vector_clock, final_vector_clock):
                log.warning(
                    "Refusing to clear fraud order_id=%s local_vc=%s final_vc=%s",
                    order_id,
                    state.vector_clock,
                    final_vector_clock,
                )
                return False

            del self._orders[order_id]

        log.info("Cleared fraud order_id=%s", order_id)
        return True

    def _get_order_clock(self, order_id):
        with self._lock:
            state = self._orders.get(order_id)
            if state is None:
                return {}
            return copy.deepcopy(state.vector_clock)

    def _run_event(self, order_id, event_name, handler, incoming_clock=None):
        with self._lock:
            state = self._orders.get(order_id)
            if state is None:
                result = EventResult(
                    success=False,
                    reason="Order not initialized",
                    event_name=event_name,
                    vector_clock={},
                )
                self._log_event(order_id, event_name, result.vector_clock, result.success, result.reason)
                return result

            self._merge_clock(state.vector_clock, incoming_clock)
            state.vector_clock[self._service_name] += 1
            order_snapshot = copy.deepcopy(state.order)
            event_clock = copy.deepcopy(state.vector_clock)

        success, reason, payload = handler(order_snapshot)

        with self._lock:
            state = self._orders.get(order_id)
            if state is not None:
                state.event_status[event_name] = success
                if not success:
                    state.failure_reason = reason

        self._log_event(order_id, event_name, event_clock, success, reason)
        return EventResult(
            success=success,
            reason=reason,
            event_name=event_name,
            vector_clock=event_clock,
            payload=payload,
        )

    def _event_d_check_user_data_for_fraud(self, order):
        user = order.get("user", {}) or {}
        billing = order.get("billingAddress", {}) or {}
        suspicious_tokens = ("fraud", "scam", "fake", "test user")

        searchable_chunks = [
            str(user.get("name", "")),
            str(user.get("contact", "")),
            str(billing.get("street", "")),
            str(billing.get("city", "")),
            str(billing.get("country", "")),
        ]
        searchable_text = " ".join(searchable_chunks).lower()

        if any(token in searchable_text for token in suspicious_tokens):
            return False, "Suspicious user data", {}

        return True, "User data looks safe", {}

    def _event_e_check_credit_card_for_fraud(self, order):
        credit = order.get("creditCard", {}) or {}
        number = str(credit.get("number", ""))
        sanitized_number = re.sub(r"\D", "", number)

        if sanitized_number and not re.fullmatch(r"\d{13,19}", sanitized_number):
            return False, "Suspicious card number", {}

        if sanitized_number and len(set(sanitized_number)) == 1:
            return False, "Suspicious repeated card digits", {}

        return True, "Credit card data looks safe", {}

    def _legacy_volume_rule(self, order):
        items = order.get("items", [])

        total_qty = 0
        for item in items:
            try:
                total_qty += int(item.get("quantity", 0))
            except (TypeError, ValueError, AttributeError):
                continue

        if total_qty >= 50:
            return False, "Too many items"

        return True, "Order volume acceptable"

    def run_event_d(self, order_id, incoming_clock=None):
        return self._run_event(order_id, "d", self._event_d_check_user_data_for_fraud, incoming_clock)

    def run_event_e(self, order_id, incoming_clock=None):
        return self._run_event(order_id, "e", self._event_e_check_credit_card_for_fraud, incoming_clock)

    def InitializeOrder(self, request, context):
        try:
            order = json.loads(request.order_json)
        except Exception as exc:
            log.warning("Invalid JSON payload received during initialization: %s", exc)
            return fd_pb2.OrderInitializationResponse(
                accepted=False,
                reason="Invalid JSON",
                vector_clock={},
            )

        vector_clock = self.cache_order(request.order_id, order, dict(request.vector_clock))
        return fd_pb2.OrderInitializationResponse(
            accepted=True,
            reason="Order cached",
            vector_clock=vector_clock,
        )

    def CheckUserFraud(self, request, context):
        result = self.run_event_d(request.order_id, dict(request.vector_clock))
        return fd_pb2.OrderEventResponse(
            success=result.success,
            reason=result.reason,
            event_name=result.event_name,
            vector_clock=result.vector_clock,
        )

    def CheckCardFraud(self, request, context):
        result = self.run_event_e(request.order_id, dict(request.vector_clock))
        return fd_pb2.OrderEventResponse(
            success=result.success,
            reason=result.reason,
            event_name=result.event_name,
            vector_clock=result.vector_clock,
        )

    def ClearOrder(self, request, context):
        final_vector_clock = dict(request.final_vector_clock)
        current_clock = self._get_order_clock(request.order_id)
        cleared = self.clear_order(request.order_id, final_vector_clock or None)
        return fd_pb2.OrderClearResponse(
            cleared=cleared,
            reason="Order cleared" if cleared else "Order was not cleared",
            vector_clock=current_clock,
        )

    def CheckFraud(self, request, context):
        log.info("CheckFraud called")

        try:
            order = json.loads(request.order_json)
        except Exception as exc:
            log.warning("Invalid JSON payload received: %s", exc)
            return fd_pb2.FraudResponse(fraud_detected=True, reason="Invalid JSON")

        log.info("Fraud check request summary: %s", summarize_order(order))

        legacy_order_id = f"legacy-fraud-{uuid4()}"
        self.cache_order(legacy_order_id, order)

        try:
            for event_runner in (self.run_event_d, self.run_event_e):
                event_result = event_runner(legacy_order_id)
                if not event_result.success:
                    return fd_pb2.FraudResponse(
                        fraud_detected=True,
                        reason=event_result.reason,
                    )

            is_valid_volume, reason = self._legacy_volume_rule(order)
            if not is_valid_volume:
                with self._lock:
                    current_clock = copy.deepcopy(self._orders[legacy_order_id].vector_clock)
                self._log_event(legacy_order_id, "legacy_volume", current_clock, False, reason)
                return fd_pb2.FraudResponse(fraud_detected=True, reason=reason)

            log.info("Fraud check completed successfully")
            return fd_pb2.FraudResponse(fraud_detected=False, reason="OK")
        finally:
            self.clear_order(legacy_order_id)


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    fd_grpc.add_FraudDetectionServiceServicer_to_server(FraudDetectionService(), server)

    port = "50051"
    server.add_insecure_port("[::]:" + port)
    server.start()
    log.info("Fraud detection service started on port %s with max_workers=%s", port, 10)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()