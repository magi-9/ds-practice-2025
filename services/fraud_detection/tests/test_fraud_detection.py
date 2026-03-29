import json
import os
import sys


FILE = __file__
pb_root = os.path.abspath(os.path.join(FILE, "../../../../utils/pb"))
sys.path.insert(0, pb_root)

from fraud_detection import fraud_detection_pb2 as fd_pb2

sys.path.insert(0, os.path.abspath(os.path.join(FILE, "../../src")))
from app import FraudDetectionService


def make_valid_order():
    return {
        "items": [{"name": "Book A", "quantity": 2}],
        "user": {"name": "John Doe", "contact": "john@example.com"},
        "billingAddress": {
            "street": "Main St 1",
            "city": "Brno",
            "country": "CZ",
        },
        "creditCard": {
            "number": "4532015112830366",
            "expirationDate": "12/25",
            "cvv": "123",
        },
    }


class TestFraudDetectionService:
    def setup_method(self):
        self.service = FraudDetectionService()
        self.context = None

    def test_legacy_check_fraud_valid_order(self):
        request = fd_pb2.OrderRequest(order_json=json.dumps(make_valid_order()))
        response = self.service.CheckFraud(request, self.context)

        assert response.fraud_detected is False
        assert response.reason == "OK"

    def test_legacy_check_fraud_detects_suspicious_user_data(self):
        order = make_valid_order()
        order["user"]["name"] = "fraud account"

        request = fd_pb2.OrderRequest(order_json=json.dumps(order))
        response = self.service.CheckFraud(request, self.context)

        assert response.fraud_detected is True
        assert response.reason == "Suspicious user data"

    def test_legacy_check_fraud_detects_repeated_digits_card(self):
        order = make_valid_order()
        order["creditCard"]["number"] = "1111111111111111"

        request = fd_pb2.OrderRequest(order_json=json.dumps(order))
        response = self.service.CheckFraud(request, self.context)

        assert response.fraud_detected is True
        assert response.reason == "Suspicious repeated card digits"

    def test_legacy_check_fraud_invalid_json(self):
        request = fd_pb2.OrderRequest(order_json="not json")
        response = self.service.CheckFraud(request, self.context)

        assert response.fraud_detected is True
        assert response.reason == "Invalid JSON"


class TestFraudEventOrderingRpcs:
    def setup_method(self):
        self.service = FraudDetectionService()
        self.context = None

    def test_initialize_order_rejects_invalid_json(self):
        request = fd_pb2.OrderInitializationRequest(
            order_id="order-1",
            order_json="not json",
            vector_clock={},
        )
        response = self.service.InitializeOrder(request, self.context)

        assert response.accepted is False
        assert response.reason == "Invalid JSON"
        assert dict(response.vector_clock) == {}

    def test_check_user_fraud_for_missing_order_id(self):
        request = fd_pb2.OrderEventRequest(order_id="missing", vector_clock={})
        response = self.service.CheckUserFraud(request, self.context)

        assert response.success is False
        assert response.reason == "Order not initialized"
        assert dict(response.vector_clock) == {}

    def test_check_card_fraud_merges_clock_and_increments_local_slot(self):
        order = make_valid_order()
        init_response = self.service.InitializeOrder(
            fd_pb2.OrderInitializationRequest(
                order_id="order-2",
                order_json=json.dumps(order),
                vector_clock={"transaction_verification": 2, "fraud_detection": 0, "suggestions": 0},
            ),
            self.context,
        )
        assert init_response.accepted is True

        event_response = self.service.CheckCardFraud(
            fd_pb2.OrderEventRequest(
                order_id="order-2",
                vector_clock={"transaction_verification": 2, "fraud_detection": 0, "suggestions": 4},
            ),
            self.context,
        )

        assert event_response.success is True
        assert event_response.event_name == "e"
        assert dict(event_response.vector_clock) == {
            "transaction_verification": 2,
            "fraud_detection": 1,
            "suggestions": 4,
        }

    def test_clear_order_refuses_when_final_clock_is_behind(self):
        order = make_valid_order()
        self.service.InitializeOrder(
            fd_pb2.OrderInitializationRequest(
                order_id="order-3",
                order_json=json.dumps(order),
                vector_clock={},
            ),
            self.context,
        )
        self.service.CheckUserFraud(
            fd_pb2.OrderEventRequest(order_id="order-3", vector_clock={}),
            self.context,
        )

        clear_response = self.service.ClearOrder(
            fd_pb2.OrderClearRequest(
                order_id="order-3",
                final_vector_clock={"transaction_verification": 0, "fraud_detection": 0, "suggestions": 0},
            ),
            self.context,
        )

        assert clear_response.cleared is False
        assert dict(clear_response.vector_clock)["fraud_detection"] == 1

    def test_clear_order_succeeds_with_up_to_date_final_clock(self):
        order = make_valid_order()
        self.service.InitializeOrder(
            fd_pb2.OrderInitializationRequest(
                order_id="order-4",
                order_json=json.dumps(order),
                vector_clock={},
            ),
            self.context,
        )
        self.service.CheckUserFraud(
            fd_pb2.OrderEventRequest(order_id="order-4", vector_clock={}),
            self.context,
        )

        clear_response = self.service.ClearOrder(
            fd_pb2.OrderClearRequest(
                order_id="order-4",
                final_vector_clock={"transaction_verification": 0, "fraud_detection": 1, "suggestions": 0},
            ),
            self.context,
        )

        assert clear_response.cleared is True
