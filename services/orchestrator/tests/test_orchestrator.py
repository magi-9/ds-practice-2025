import json
import os
import sys
from unittest.mock import patch

import pytest


FILE = __file__
pb_root = os.path.abspath(os.path.join(FILE, "../../../../utils/pb"))
sys.path.insert(0, pb_root)

sys.path.insert(0, os.path.abspath(os.path.join(FILE, "../../src")))
from app import BackendServiceError, app, mask_sensitive_data


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as flask_client:
        yield flask_client


@pytest.fixture
def valid_order():
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


class TestHealthAndValidation:
    def test_index_endpoint(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert b"Orchestrator is running" in response.data

    def test_invalid_json_body(self, client):
        response = client.post("/checkout", data="not json", content_type="text/plain")
        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["error"]["code"] == "INVALID_JSON"

    def test_missing_credit_card(self, client, valid_order):
        del valid_order["creditCard"]
        response = client.post("/checkout", json=valid_order)
        assert response.status_code == 400
        data = json.loads(response.data)
        assert data["error"]["code"] == "MISSING_CREDIT_CARD"


class TestMaskSensitiveData:
    def test_masks_card_number_and_cvv(self):
        payload = {
            "creditCard": {
                "number": "4532015112830366",
                "cvv": "123",
            }
        }
        masked = mask_sensitive_data(payload)
        assert masked["creditCard"]["number"] == "****0366"
        assert masked["creditCard"]["cvv"] == "***"


class TestCheckoutEventFlow:
    @patch("app.clear_all_services")
    @patch("app.sg_event_generate")
    @patch("app.fd_event")
    @patch("app.tv_event")
    @patch("app.init_all_services")
    def test_order_approved_with_event_ordering_flow(
        self,
        mock_init,
        mock_tv_event,
        mock_fd_event,
        mock_sg_event,
        mock_clear,
        client,
        valid_order,
    ):
        mock_init.return_value = {
            "transaction_verification": 0,
            "fraud_detection": 0,
            "suggestions": 0,
        }

        def tv_side_effect(order_id, method_name, vc, request_id):
            if method_name in ("VerifyItemsNonEmpty", "VerifyUserData"):
                return True, "OK", method_name, {
                    "transaction_verification": 1,
                    "fraud_detection": 0,
                    "suggestions": 0,
                }
            if method_name == "VerifyCreditCard":
                return True, "OK", method_name, {
                    "transaction_verification": 2,
                    "fraud_detection": 0,
                    "suggestions": 0,
                }
            raise AssertionError(f"Unexpected TV method: {method_name}")

        def fd_side_effect(order_id, method_name, vc, request_id):
            if method_name == "CheckUserFraud":
                return True, "OK", method_name, {
                    "transaction_verification": 2,
                    "fraud_detection": 1,
                    "suggestions": 0,
                }
            if method_name == "CheckCardFraud":
                return True, "OK", method_name, {
                    "transaction_verification": 2,
                    "fraud_detection": 2,
                    "suggestions": 0,
                }
            raise AssertionError(f"Unexpected FD method: {method_name}")

        mock_tv_event.side_effect = tv_side_effect
        mock_fd_event.side_effect = fd_side_effect
        mock_sg_event.return_value = (
            True,
            "OK",
            "f",
            {
                "transaction_verification": 2,
                "fraud_detection": 2,
                "suggestions": 1,
            },
            [{"bookId": "101", "title": "1984", "author": "George Orwell"}],
        )

        response = client.post("/checkout", json=valid_order)
        assert response.status_code == 200

        data = json.loads(response.data)
        assert data["status"] == "Order Approved"
        assert len(data["suggestedBooks"]) == 1
        assert data["suggestedBooks"][0]["bookId"] == "101"
        assert data["orderId"]

        mock_init.assert_called_once()
        mock_clear.assert_called_once()

    @patch("app.clear_all_services")
    @patch("app.tv_event")
    @patch("app.init_all_services")
    def test_init_failure_returns_503_with_reason_and_cleans_up(
        self,
        mock_init,
        mock_tv_event,
        mock_clear,
        client,
        valid_order,
    ):
        del mock_tv_event
        mock_init.side_effect = RuntimeError("TV init rejected: Invalid JSON")

        response = client.post("/checkout", json=valid_order)
        assert response.status_code == 503

        data = json.loads(response.data)
        assert data["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "Failed to initialize backend services" in data["error"]["message"]
        assert "TV init rejected: Invalid JSON" in data["error"]["message"]
        mock_clear.assert_called_once()

    @patch("app.clear_all_services")
    @patch("app.tv_event")
    @patch("app.init_all_services")
    def test_backend_event_transport_error_returns_503_and_cleans_up(
        self,
        mock_init,
        mock_tv_event,
        mock_clear,
        client,
        valid_order,
    ):
        mock_init.return_value = {
            "transaction_verification": 0,
            "fraud_detection": 0,
            "suggestions": 0,
        }

        def tv_side_effect(order_id, method_name, vc, request_id):
            if method_name == "VerifyItemsNonEmpty":
                raise BackendServiceError("transaction_verification.VerifyItemsNonEmpty unavailable")
            return True, "OK", method_name, vc

        mock_tv_event.side_effect = tv_side_effect

        response = client.post("/checkout", json=valid_order)
        assert response.status_code == 503

        data = json.loads(response.data)
        assert data["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "transaction_verification.VerifyItemsNonEmpty unavailable" in data["error"]["message"]
        mock_clear.assert_called_once()

    @patch("app.clear_all_services")
    @patch("app.sg_event_generate")
    @patch("app.fd_event")
    @patch("app.tv_event")
    @patch("app.init_all_services")
    def test_business_rejection_path_returns_200(
        self,
        mock_init,
        mock_tv_event,
        mock_fd_event,
        mock_sg_event,
        mock_clear,
        client,
        valid_order,
    ):
        del mock_fd_event
        del mock_sg_event
        mock_init.return_value = {
            "transaction_verification": 0,
            "fraud_detection": 0,
            "suggestions": 0,
        }

        def tv_side_effect(order_id, method_name, vc, request_id):
            if method_name == "VerifyItemsNonEmpty":
                return True, "Items list is valid", "a", {
                    "transaction_verification": 1,
                    "fraud_detection": 0,
                    "suggestions": 0,
                }
            if method_name == "VerifyUserData":
                return False, "Missing user name", "b", {
                    "transaction_verification": 1,
                    "fraud_detection": 0,
                    "suggestions": 0,
                }
            raise AssertionError(f"Unexpected TV method for rejection path: {method_name}")

        mock_tv_event.side_effect = tv_side_effect

        response = client.post("/checkout", json=valid_order)
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["status"] == "Order Rejected"
        assert data["suggestedBooks"] == []
        mock_clear.assert_called_once()
