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

from fraud_detection import fraud_detection_pb2 as fd_pb2
from fraud_detection import fraud_detection_pb2_grpc as fd_grpc

import grpc
from concurrent import futures

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("fraud_detection")


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

# Create a class to define the server functions, derived from
# fraud_detection_pb2_grpc.FraudDetectionServiceServicer
class FraudDetectionService(fd_grpc.FraudDetectionServiceServicer):
    def CheckFraud(self, request, context):
        log.info("CheckFraud called")

        try:
            order = json.loads(request.order_json)
        except Exception as exc:
            log.warning("Invalid JSON payload received: %s", exc)
            return fd_pb2.FraudResponse(fraud_detected=True, reason="Invalid JSON")

        log.info("Fraud check request summary: %s", summarize_order(order))

        items = order.get("items", [])
        user = order.get("user", {}) or {}
        credit = order.get("creditCard", {}) or {}

        # Dummy fraud rules
        # 1) Large quantity
        total_qty = 0
        for it in items:
            try:
                total_qty += int(it.get("quantity", 0))
            except Exception:
                pass
        if total_qty >= 50:
            log.warning("Fraud detected: total quantity too high (%s)", total_qty)
            return fd_pb2.FraudResponse(fraud_detected=True, reason="Too many items")

        # 2) Weird card number format (very basic)
        number = str(credit.get("number", ""))
        if number and not re.fullmatch(r"\d{13,19}", number):
            log.warning("Fraud detected: suspicious card number ending with %s", number[-4:])
            return fd_pb2.FraudResponse(fraud_detected=True, reason="Suspicious card number")

        log.info("Fraud check completed successfully")
        return fd_pb2.FraudResponse(fraud_detected=False, reason="OK")

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