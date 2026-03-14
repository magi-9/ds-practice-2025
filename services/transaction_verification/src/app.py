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

import transaction_verification.transaction_verification_pb2 as tv_pb2
import transaction_verification.transaction_verification_pb2_grpc as tv_grpc

import grpc
from concurrent import futures

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("transaction_verification")

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
    user = order.get("user") or {}
    credit = order.get("creditCard") or {}
    card_number = str(credit.get("number", ""))

    return {
        "item_count": len(items),
        "has_user_name": bool(user.get("name")),
        "has_user_contact": bool(user.get("contact")),
        "has_expiration_date": bool(credit.get("expirationDate")),
        "has_cvv": bool(credit.get("cvv")),
        "card_suffix": card_number[-4:] if card_number else None,
    }


class TransactionVerificationService(tv_grpc.TransactionVerificationServiceServicer):
    def __init__(self):
        self._orders: dict[str, OrderState] = {}
        self._lock = RLock()
        self._service_name = "transaction_verification"

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

        log.info("Cached verification order_id=%s vc=%s", order_id, clock_snapshot)
        return clock_snapshot

    def clear_order(self, order_id, final_vector_clock=None):
        with self._lock:
            state = self._orders.get(order_id)
            if state is None:
                return False

            if final_vector_clock and not self._clock_lte(state.vector_clock, final_vector_clock):
                log.warning(
                    "Refusing to clear verification order_id=%s local_vc=%s final_vc=%s",
                    order_id,
                    state.vector_clock,
                    final_vector_clock,
                )
                return False

            del self._orders[order_id]

        log.info("Cleared verification order_id=%s", order_id)
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

    def _event_a_validate_items(self, order):
        items = order.get("items", [])
        if not isinstance(items, list) or len(items) == 0:
            return False, "No items in order", {}
        return True, "Items list is valid", {}

    def _event_b_validate_user_data(self, order):
        user = order.get("user", {}) or {}
        billing = order.get("billingAddress", {}) or {}

        if not user.get("name"):
            return False, "Missing user name", {}
        if not user.get("contact"):
            return False, "Missing user contact", {}
        if not isinstance(billing, dict) or not billing.get("street"):
            return False, "Missing billing street", {}
        if not billing.get("city"):
            return False, "Missing billing city", {}
        if not billing.get("country"):
            return False, "Missing billing country", {}

        return True, "User data is complete", {}

    def _event_c_validate_credit_card(self, order):
        credit = order.get("creditCard", {}) or {}

        if not credit.get("number"):
            return False, "Missing credit card number", {}
        if not credit.get("expirationDate"):
            return False, "Missing expiration date", {}
        if not credit.get("cvv"):
            return False, "Missing CVV", {}

        card_number = str(credit.get("number", ""))
        if not re.fullmatch(r"\d{13,19}", card_number):
            return False, "Invalid credit card format", {}

        cvv = str(credit.get("cvv", ""))
        if not re.fullmatch(r"\d{3,4}", cvv):
            return False, "Invalid CVV format", {}

        return True, "Credit card data is valid", {}

    def _legacy_validate_item_details(self, order):
        items = order.get("items", [])

        for item in items:
            if not item.get("name"):
                return False, "Item missing name"

            quantity = item.get("quantity")
            if quantity is None or not isinstance(quantity, (int, float)) or quantity <= 0:
                return False, "Invalid item quantity"

        return True, "Item details are valid"

    def run_event_a(self, order_id, incoming_clock=None):
        return self._run_event(order_id, "a", self._event_a_validate_items, incoming_clock)

    def run_event_b(self, order_id, incoming_clock=None):
        return self._run_event(order_id, "b", self._event_b_validate_user_data, incoming_clock)

    def run_event_c(self, order_id, incoming_clock=None):
        return self._run_event(order_id, "c", self._event_c_validate_credit_card, incoming_clock)

    def InitializeOrder(self, request, context):
        try:
            order = json.loads(request.order_json)
        except Exception as exc:
            log.warning("Invalid JSON payload received during initialization: %s", exc)
            return tv_pb2.OrderInitializationResponse(
                accepted=False,
                reason="Invalid JSON",
                vector_clock={},
            )

        vector_clock = self.cache_order(request.order_id, order, dict(request.vector_clock))
        return tv_pb2.OrderInitializationResponse(
            accepted=True,
            reason="Order cached",
            vector_clock=vector_clock,
        )

    def VerifyItemsNonEmpty(self, request, context):
        result = self.run_event_a(request.order_id, dict(request.vector_clock))
        return tv_pb2.OrderEventResponse(
            success=result.success,
            reason=result.reason,
            event_name=result.event_name,
            vector_clock=result.vector_clock,
        )

    def VerifyUserData(self, request, context):
        result = self.run_event_b(request.order_id, dict(request.vector_clock))
        return tv_pb2.OrderEventResponse(
            success=result.success,
            reason=result.reason,
            event_name=result.event_name,
            vector_clock=result.vector_clock,
        )

    def VerifyCreditCard(self, request, context):
        result = self.run_event_c(request.order_id, dict(request.vector_clock))
        return tv_pb2.OrderEventResponse(
            success=result.success,
            reason=result.reason,
            event_name=result.event_name,
            vector_clock=result.vector_clock,
        )

    def ClearOrder(self, request, context):
        final_vector_clock = dict(request.final_vector_clock)
        current_clock = self._get_order_clock(request.order_id)
        cleared = self.clear_order(request.order_id, final_vector_clock or None)
        return tv_pb2.OrderClearResponse(
            cleared=cleared,
            reason="Order cleared" if cleared else "Order was not cleared",
            vector_clock=current_clock,
        )

    def VerifyTransaction(self, request, context):
        log.info("VerifyTransaction called")

        try:
            order = json.loads(request.order_json)
        except Exception as exc:
            log.warning("Invalid JSON payload received: %s", exc)
            return tv_pb2.TransactionResponse(is_valid=False, reason="Invalid JSON")

        log.info("Transaction verification request summary: %s", summarize_order(order))

        legacy_order_id = f"legacy-transaction-{uuid4()}"
        self.cache_order(legacy_order_id, order)

        try:
            for event_runner in (self.run_event_a, self.run_event_b, self.run_event_c):
                event_result = event_runner(legacy_order_id)
                if not event_result.success:
                    return tv_pb2.TransactionResponse(
                        is_valid=False,
                        reason=event_result.reason,
                    )

            items_ok, reason = self._legacy_validate_item_details(order)
            if not items_ok:
                with self._lock:
                    current_clock = copy.deepcopy(self._orders[legacy_order_id].vector_clock)
                self._log_event(legacy_order_id, "legacy_item_details", current_clock, False, reason)
                return tv_pb2.TransactionResponse(is_valid=False, reason=reason)

            log.info("Transaction verification completed successfully")
            return tv_pb2.TransactionResponse(is_valid=True, reason="Transaction valid")
        finally:
            self.clear_order(legacy_order_id)


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    tv_grpc.add_TransactionVerificationServiceServicer_to_server(
        TransactionVerificationService(), server
    )

    port = "50052"
    server.add_insecure_port("[::]:" + port)
    server.start()
    log.info("Transaction verification service started on port %s with max_workers=%s", port, 10)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
