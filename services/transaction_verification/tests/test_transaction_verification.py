import json
import os
import sys


FILE = __file__
pb_root = os.path.abspath(os.path.join(FILE, "../../../../utils/pb"))
sys.path.insert(0, pb_root)

from transaction_verification import transaction_verification_pb2 as tv_pb2

sys.path.insert(0, os.path.abspath(os.path.join(FILE, "../../src")))
from app import TransactionVerificationService


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


class TestTransactionVerificationService:
    def setup_method(self):
        self.service = TransactionVerificationService()
        self.context = None

    def test_legacy_verify_transaction_valid_order(self):
        request = tv_pb2.TransactionRequest(order_json=json.dumps(make_valid_order()))
        response = self.service.VerifyTransaction(request, self.context)

        assert response.is_valid is True
        assert response.reason == "Transaction valid"

    def test_legacy_verify_transaction_invalid_json(self):
        request = tv_pb2.TransactionRequest(order_json="not json")
        response = self.service.VerifyTransaction(request, self.context)

        assert response.is_valid is False
        assert response.reason == "Invalid JSON"

    def test_legacy_verify_transaction_missing_billing_street(self):
        order = make_valid_order()
        order["billingAddress"].pop("street")

        request = tv_pb2.TransactionRequest(order_json=json.dumps(order))
        response = self.service.VerifyTransaction(request, self.context)

        assert response.is_valid is False
        assert response.reason == "Missing billing street"

    def test_legacy_verify_transaction_invalid_card_format(self):
        order = make_valid_order()
        order["creditCard"]["number"] = "1234-5678"

        request = tv_pb2.TransactionRequest(order_json=json.dumps(order))
        response = self.service.VerifyTransaction(request, self.context)

        assert response.is_valid is False
        assert response.reason == "Invalid credit card format"


class TestTransactionEventOrderingRpcs:
    def setup_method(self):
        self.service = TransactionVerificationService()
        self.context = None

    def test_initialize_order_rejects_invalid_json(self):
        request = tv_pb2.OrderInitializationRequest(
            order_id="order-1",
            order_json="not json",
            vector_clock={},
        )
        response = self.service.InitializeOrder(request, self.context)

        assert response.accepted is False
        assert response.reason == "Invalid JSON"
        assert dict(response.vector_clock) == {}

    def test_verify_items_non_empty_for_missing_order_id(self):
        request = tv_pb2.OrderEventRequest(order_id="missing", vector_clock={})
        response = self.service.VerifyItemsNonEmpty(request, self.context)

        assert response.success is False
        assert response.reason == "Order not initialized"
        assert dict(response.vector_clock) == {}

    def test_verify_user_data_merges_vector_clock_and_increments_local_slot(self):
        order = make_valid_order()
        init_response = self.service.InitializeOrder(
            tv_pb2.OrderInitializationRequest(
                order_id="order-2",
                order_json=json.dumps(order),
                vector_clock={"transaction_verification": 0, "fraud_detection": 3, "suggestions": 0},
            ),
            self.context,
        )
        assert init_response.accepted is True

        event_response = self.service.VerifyUserData(
            tv_pb2.OrderEventRequest(
                order_id="order-2",
                vector_clock={"transaction_verification": 0, "fraud_detection": 3, "suggestions": 2},
            ),
            self.context,
        )

        assert event_response.success is True
        assert event_response.event_name == "b"
        assert dict(event_response.vector_clock) == {
            "transaction_verification": 1,
            "fraud_detection": 3,
            "suggestions": 2,
        }

    def test_verify_credit_card_increments_after_previous_event(self):
        order = make_valid_order()
        self.service.InitializeOrder(
            tv_pb2.OrderInitializationRequest(
                order_id="order-3",
                order_json=json.dumps(order),
                vector_clock={},
            ),
            self.context,
        )

        first = self.service.VerifyItemsNonEmpty(
            tv_pb2.OrderEventRequest(order_id="order-3", vector_clock={}),
            self.context,
        )
        second = self.service.VerifyCreditCard(
            tv_pb2.OrderEventRequest(order_id="order-3", vector_clock=dict(first.vector_clock)),
            self.context,
        )

        assert first.success is True
        assert second.success is True
        assert dict(second.vector_clock)["transaction_verification"] == 2

    def test_clear_order_refuses_when_final_clock_is_behind(self):
        order = make_valid_order()
        self.service.InitializeOrder(
            tv_pb2.OrderInitializationRequest(
                order_id="order-4",
                order_json=json.dumps(order),
                vector_clock={},
            ),
            self.context,
        )
        self.service.VerifyItemsNonEmpty(
            tv_pb2.OrderEventRequest(order_id="order-4", vector_clock={}),
            self.context,
        )

        clear_response = self.service.ClearOrder(
            tv_pb2.OrderClearRequest(
                order_id="order-4",
                final_vector_clock={"transaction_verification": 0, "fraud_detection": 0, "suggestions": 0},
            ),
            self.context,
        )

        assert clear_response.cleared is False
        assert dict(clear_response.vector_clock)["transaction_verification"] == 1

    def test_clear_order_succeeds_with_equal_clock(self):
        order = make_valid_order()
        self.service.InitializeOrder(
            tv_pb2.OrderInitializationRequest(
                order_id="order-5",
                order_json=json.dumps(order),
                vector_clock={},
            ),
            self.context,
        )
        self.service.VerifyItemsNonEmpty(
            tv_pb2.OrderEventRequest(order_id="order-5", vector_clock={}),
            self.context,
        )

        clear_response = self.service.ClearOrder(
            tv_pb2.OrderClearRequest(
                order_id="order-5",
                final_vector_clock={"transaction_verification": 1, "fraud_detection": 0, "suggestions": 0},
            ),
            self.context,
        )

        assert clear_response.cleared is True
