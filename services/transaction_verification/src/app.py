import sys
import os
import json
import re
import logging

# This set of lines are needed to import the gRPC stubs.
# The path of the stubs is relative to the current file, or absolute inside the container.
# Change these lines only if strictly needed.
FILE = __file__ if '__file__' in globals() else os.getenv("PYTHONFILE", "")
pb_root = os.path.abspath(os.path.join(FILE, "../../../../utils/pb"))
sys.path.insert(0, pb_root)

from transaction_verification import transaction_verification_pb2 as tv_pb2
from transaction_verification import transaction_verification_pb2_grpc as tv_grpc

import grpc
from concurrent import futures

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("transaction_verification")


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
    def VerifyTransaction(self, request, context):
        log.info("VerifyTransaction called")

        try:
            order = json.loads(request.order_json)
        except Exception as exc:
            log.warning("Invalid JSON payload received: %s", exc)
            return tv_pb2.TransactionResponse(is_valid=False, reason="Invalid JSON")

        log.info("Transaction verification request summary: %s", summarize_order(order))

        items = order.get("items", [])
        user = order.get("user", {}) or {}
        credit = order.get("creditCard", {}) or {}

        # Validation rules
        # 1) Check if items list is not empty
        if not items or len(items) == 0:
            log.warning("Transaction invalid: order has no items")
            return tv_pb2.TransactionResponse(is_valid=False, reason="No items in order")

        # 2) Check if user info is complete
        if not user.get("name"):
            log.warning("Transaction invalid: missing user name")
            return tv_pb2.TransactionResponse(is_valid=False, reason="Missing user name")
        if not user.get("contact"):
            log.warning("Transaction invalid: missing user contact")
            return tv_pb2.TransactionResponse(is_valid=False, reason="Missing user contact")

        # 3) Check if credit card info is complete
        if not credit.get("number"):
            log.warning("Transaction invalid: missing credit card number")
            return tv_pb2.TransactionResponse(is_valid=False, reason="Missing credit card number")
        if not credit.get("expirationDate"):
            log.warning("Transaction invalid: missing expiration date")
            return tv_pb2.TransactionResponse(is_valid=False, reason="Missing expiration date")
        if not credit.get("cvv"):
            log.warning("Transaction invalid: missing CVV")
            return tv_pb2.TransactionResponse(is_valid=False, reason="Missing CVV")

        # 4) Validate credit card number format (13-19 digits)
        card_number = str(credit.get("number", ""))
        if not re.fullmatch(r"\d{13,19}", card_number):
            log.warning("Transaction invalid: malformed card number ending with %s", card_number[-4:])
            return tv_pb2.TransactionResponse(is_valid=False, reason="Invalid credit card format")

        # 5) Validate CVV format (3-4 digits)
        cvv = str(credit.get("cvv", ""))
        if not re.fullmatch(r"\d{3,4}", cvv):
            log.warning("Transaction invalid: malformed CVV")
            return tv_pb2.TransactionResponse(is_valid=False, reason="Invalid CVV format")

        # 6) Check each item has required fields
        for item in items:
            if not item.get("name"):
                log.warning("Transaction invalid: item missing name")
                return tv_pb2.TransactionResponse(is_valid=False, reason="Item missing name")
            quantity = item.get("quantity")
            if quantity is None or not isinstance(quantity, (int, float)) or quantity <= 0:
                log.warning("Transaction invalid: item has invalid quantity=%s", quantity)
                return tv_pb2.TransactionResponse(is_valid=False, reason="Invalid item quantity")

        # All checks passed
        log.info("Transaction verification completed successfully")
        return tv_pb2.TransactionResponse(is_valid=True, reason="Transaction valid")


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
