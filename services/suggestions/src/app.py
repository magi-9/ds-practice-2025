import sys
import os
import json
import copy
import random
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

import suggestions.suggestions_pb2 as sg_pb2
import suggestions.suggestions_pb2_grpc as sg_grpc

import grpc
from concurrent import futures

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("suggestions")

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

    return {
        "item_count": len(items),
        "has_user_name": bool(user.get("name")),
        "has_user_contact": bool(user.get("contact")),
    }


BOOK_CATALOG = [
    {"book_id": "101", "title": "The Great Gatsby", "author": "F. Scott Fitzgerald"},
    {"book_id": "102", "title": "To Kill a Mockingbird", "author": "Harper Lee"},
    {"book_id": "103", "title": "1984", "author": "George Orwell"},
    {"book_id": "104", "title": "Pride and Prejudice", "author": "Jane Austen"},
    {"book_id": "105", "title": "The Catcher in the Rye", "author": "J.D. Salinger"},
    {"book_id": "106", "title": "Animal Farm", "author": "George Orwell"},
    {"book_id": "107", "title": "Lord of the Flies", "author": "William Golding"},
    {"book_id": "108", "title": "Brave New World", "author": "Aldous Huxley"},
    {"book_id": "109", "title": "The Hobbit", "author": "J.R.R. Tolkien"},
    {"book_id": "110", "title": "Fahrenheit 451", "author": "Ray Bradbury"},
]


class SuggestionsService(sg_grpc.SuggestionsServiceServicer):
    def __init__(self):
        self._orders: dict[str, OrderState] = {}
        self._lock = RLock()
        self._service_name = "suggestions"

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

        log.info("Cached suggestions order_id=%s vc=%s", order_id, clock_snapshot)
        return clock_snapshot

    def clear_order(self, order_id, final_vector_clock=None):
        with self._lock:
            state = self._orders.get(order_id)
            if state is None:
                return False

            if final_vector_clock and not self._clock_lte(state.vector_clock, final_vector_clock):
                log.warning(
                    "Refusing to clear suggestions order_id=%s local_vc=%s final_vc=%s",
                    order_id,
                    state.vector_clock,
                    final_vector_clock,
                )
                return False

            del self._orders[order_id]

        log.info("Cleared suggestions order_id=%s", order_id)
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

    def _event_f_generate_suggestions(self, order):
        num_suggestions = min(3, len(BOOK_CATALOG))
        suggested_books = random.sample(BOOK_CATALOG, num_suggestions)
        return True, "Suggestions generated", {"books": suggested_books}

    def run_event_f(self, order_id, incoming_clock=None):
        return self._run_event(order_id, "f", self._event_f_generate_suggestions, incoming_clock)

    def InitializeOrder(self, request, context):
        try:
            order = json.loads(request.order_json)
        except Exception as exc:
            log.warning("Invalid JSON payload received during initialization: %s", exc)
            return sg_pb2.OrderInitializationResponse(
                accepted=False,
                reason="Invalid JSON",
                vector_clock={},
            )

        vector_clock = self.cache_order(request.order_id, order, dict(request.vector_clock))
        return sg_pb2.OrderInitializationResponse(
            accepted=True,
            reason="Order cached",
            vector_clock=vector_clock,
        )

    def GenerateSuggestions(self, request, context):
        result = self.run_event_f(request.order_id, dict(request.vector_clock))
        books = [
            sg_pb2.Book(
                book_id=book["book_id"],
                title=book["title"],
                author=book["author"],
            )
            for book in result.payload.get("books", [])
        ]
        return sg_pb2.SuggestionsEventResponse(
            success=result.success,
            reason=result.reason,
            event_name=result.event_name,
            vector_clock=result.vector_clock,
            books=books,
        )

    def ClearOrder(self, request, context):
        final_vector_clock = dict(request.final_vector_clock)
        current_clock = self._get_order_clock(request.order_id)
        cleared = self.clear_order(request.order_id, final_vector_clock or None)
        return sg_pb2.OrderClearResponse(
            cleared=cleared,
            reason="Order cleared" if cleared else "Order was not cleared",
            vector_clock=current_clock,
        )

    def GetSuggestions(self, request, context):
        log.info("GetSuggestions called")

        try:
            order = json.loads(request.order_json)
        except Exception as exc:
            log.warning("Invalid JSON payload received: %s", exc)
            return sg_pb2.SuggestionsResponse(books=[])

        log.info("Suggestion request summary: %s", summarize_order(order))

        legacy_order_id = f"legacy-suggestions-{uuid4()}"
        self.cache_order(legacy_order_id, order)

        try:
            event_result = self.run_event_f(legacy_order_id)
            suggested_books = event_result.payload.get("books", [])

            books = [
                sg_pb2.Book(
                    book_id=book["book_id"],
                    title=book["title"],
                    author=book["author"],
                )
                for book in suggested_books
            ]

            log.info(
                "Returning %s book suggestions with ids=%s",
                len(books),
                [book.book_id for book in books],
            )
            return sg_pb2.SuggestionsResponse(books=books)
        finally:
            self.clear_order(legacy_order_id)


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    sg_grpc.add_SuggestionsServiceServicer_to_server(SuggestionsService(), server)

    port = "50053"
    server.add_insecure_port("[::]:" + port)
    server.start()
    log.info("Suggestions service started on port %s with max_workers=%s", port, 10)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
