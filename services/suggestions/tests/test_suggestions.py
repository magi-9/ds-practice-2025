import json
import os
import sys
from unittest.mock import patch


FILE = __file__
pb_root = os.path.abspath(os.path.join(FILE, "../../../../utils/pb"))
sys.path.insert(0, pb_root)

from suggestions import suggestions_pb2 as sg_pb2

sys.path.insert(0, os.path.abspath(os.path.join(FILE, "../../src")))
from app import BOOK_CATALOG, SuggestionsService


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


class TestLegacySuggestionsRpc:
    def setup_method(self):
        self.service = SuggestionsService()
        self.context = None

    def test_get_suggestions_returns_three_books(self):
        request = sg_pb2.SuggestionsRequest(order_json=json.dumps(make_valid_order()))
        response = self.service.GetSuggestions(request, self.context)

        assert len(response.books) == 3

    def test_get_suggestions_invalid_json_returns_empty_list(self):
        request = sg_pb2.SuggestionsRequest(order_json="not json")
        response = self.service.GetSuggestions(request, self.context)

        assert len(response.books) == 0

    def test_get_suggestions_books_are_from_catalog(self):
        request = sg_pb2.SuggestionsRequest(order_json=json.dumps(make_valid_order()))
        response = self.service.GetSuggestions(request, self.context)

        valid_ids = {book["book_id"] for book in BOOK_CATALOG}
        assert all(book.book_id in valid_ids for book in response.books)

    def test_get_suggestions_random_sampling_is_used(self):
        request = sg_pb2.SuggestionsRequest(order_json=json.dumps(make_valid_order()))

        with patch("app.random.sample") as mock_sample:
            mock_sample.return_value = [BOOK_CATALOG[0], BOOK_CATALOG[1], BOOK_CATALOG[2]]
            response = self.service.GetSuggestions(request, self.context)

        assert len(response.books) == 3
        mock_sample.assert_called_once_with(BOOK_CATALOG, 3)


class TestSuggestionsEventOrderingRpcs:
    def setup_method(self):
        self.service = SuggestionsService()
        self.context = None

    def test_initialize_order_rejects_invalid_json(self):
        request = sg_pb2.OrderInitializationRequest(
            order_id="order-1",
            order_json="not json",
            vector_clock={},
        )
        response = self.service.InitializeOrder(request, self.context)

        assert response.accepted is False
        assert response.reason == "Invalid JSON"
        assert dict(response.vector_clock) == {}

    def test_generate_suggestions_for_missing_order_id(self):
        request = sg_pb2.OrderEventRequest(order_id="missing", vector_clock={})
        response = self.service.GenerateSuggestions(request, self.context)

        assert response.success is False
        assert response.reason == "Order not initialized"
        assert dict(response.vector_clock) == {}
        assert len(response.books) == 0

    def test_generate_suggestions_merges_clock_and_increments_local_slot(self):
        order = make_valid_order()
        init_response = self.service.InitializeOrder(
            sg_pb2.OrderInitializationRequest(
                order_id="order-2",
                order_json=json.dumps(order),
                vector_clock={"transaction_verification": 2, "fraud_detection": 0, "suggestions": 0},
            ),
            self.context,
        )
        assert init_response.accepted is True

        event_response = self.service.GenerateSuggestions(
            sg_pb2.OrderEventRequest(
                order_id="order-2",
                vector_clock={"transaction_verification": 2, "fraud_detection": 1, "suggestions": 0},
            ),
            self.context,
        )

        assert event_response.success is True
        assert event_response.event_name == "f"
        assert dict(event_response.vector_clock) == {
            "transaction_verification": 2,
            "fraud_detection": 1,
            "suggestions": 1,
        }
        assert len(event_response.books) == 3

    def test_clear_order_refuses_when_final_clock_is_behind(self):
        order = make_valid_order()
        self.service.InitializeOrder(
            sg_pb2.OrderInitializationRequest(
                order_id="order-3",
                order_json=json.dumps(order),
                vector_clock={},
            ),
            self.context,
        )
        self.service.GenerateSuggestions(
            sg_pb2.OrderEventRequest(order_id="order-3", vector_clock={}),
            self.context,
        )

        clear_response = self.service.ClearOrder(
            sg_pb2.OrderClearRequest(
                order_id="order-3",
                final_vector_clock={"transaction_verification": 0, "fraud_detection": 0, "suggestions": 0},
            ),
            self.context,
        )

        assert clear_response.cleared is False
        assert dict(clear_response.vector_clock)["suggestions"] == 1

    def test_clear_order_succeeds_with_equal_clock(self):
        order = make_valid_order()
        self.service.InitializeOrder(
            sg_pb2.OrderInitializationRequest(
                order_id="order-4",
                order_json=json.dumps(order),
                vector_clock={},
            ),
            self.context,
        )
        self.service.GenerateSuggestions(
            sg_pb2.OrderEventRequest(order_id="order-4", vector_clock={}),
            self.context,
        )

        clear_response = self.service.ClearOrder(
            sg_pb2.OrderClearRequest(
                order_id="order-4",
                final_vector_clock={"transaction_verification": 0, "fraud_detection": 0, "suggestions": 1},
            ),
            self.context,
        )

        assert clear_response.cleared is True
