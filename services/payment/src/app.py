# pyright: reportMissingImports=false

import os
import sys
import json
import logging
from dataclasses import dataclass
from threading import RLock
from uuid import uuid4

import grpc
from concurrent import futures

FILE = __file__ if "__file__" in globals() else os.getenv("PYTHONFILE", "")
repo_root = os.path.abspath(os.path.join(os.path.dirname(FILE), "../../.."))
pb_root = os.path.join(repo_root, "utils", "pb")
sys.path.insert(0, pb_root)

import payment.payment_pb2 as pay_pb2
import payment.payment_pb2_grpc as pay_grpc


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("payment")


@dataclass
class TxState:
    transaction_id: str
    order_id: str
    amount: float
    currency: str
    state: int
    payment_id: str = ""
    reason: str = ""


class PaymentService(pay_grpc.PaymentServiceServicer):
    def __init__(self, state_file_path=None):
        self._lock = RLock()
        self._transactions: dict[str, TxState] = {}
        self._state_file_path = state_file_path or os.getenv(
            "PAYMENT_STATE_FILE",
            "/tmp/payment_transactions.json",
        )
        self._load_state()

    def _serialize_state(self):
        return {
            tx_id: {
                "transaction_id": state.transaction_id,
                "order_id": state.order_id,
                "amount": state.amount,
                "currency": state.currency,
                "state": int(state.state),
                "payment_id": state.payment_id,
                "reason": state.reason,
            }
            for tx_id, state in self._transactions.items()
        }

    def _persist_state(self):
        state_dir = os.path.dirname(self._state_file_path)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)

        tmp_path = f"{self._state_file_path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(self._serialize_state(), f)
        os.replace(tmp_path, self._state_file_path)

    def _load_state(self):
        if not os.path.exists(self._state_file_path):
            return

        try:
            with open(self._state_file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            log.warning("Failed to load payment state from disk path=%s error=%s", self._state_file_path, e)
            return

        loaded = 0
        for tx_id, raw in data.items():
            try:
                self._transactions[tx_id] = TxState(
                    transaction_id=str(raw.get("transaction_id", tx_id)),
                    order_id=str(raw.get("order_id", "")),
                    amount=float(raw.get("amount", 0.0)),
                    currency=str(raw.get("currency", "")),
                    state=int(raw.get("state", pay_pb2.PAYMENT_STATE_UNKNOWN)),
                    payment_id=str(raw.get("payment_id", "")),
                    reason=str(raw.get("reason", "")),
                )
                loaded += 1
            except Exception:
                continue

        log.info("Loaded persisted payment transaction state entries=%d path=%s", loaded, self._state_file_path)

    @staticmethod
    def _is_same_prepare_payload(existing: TxState, order_id: str, amount: float, currency: str):
        return (
            existing.order_id == order_id
            and existing.amount == amount
            and existing.currency == currency
        )

    def ProcessPayment(self, request, context):
        order_id = (request.order_id or "").strip()
        if not order_id:
            return pay_pb2.PaymentResponse(success=False, reason="order_id is required", payment_id="")

        payment_id = f"pay-{uuid4().hex[:10]}"
        log.info(
            "PAYMENT_EXECUTED order_id=%s amount=%.2f currency=%s payment_id=%s",
            order_id,
            request.amount,
            request.currency,
            payment_id,
        )
        return pay_pb2.PaymentResponse(success=True, reason="Payment executed", payment_id=payment_id)

    def Prepare(self, request, context):
        tx_id = (request.transaction_id or "").strip()
        order_id = (request.order_id or "").strip()
        currency = (request.currency or "").strip()

        if not tx_id:
            return pay_pb2.PrepareResponse(
                vote_commit=False,
                reason="transaction_id is required",
                participant_state=pay_pb2.PAYMENT_STATE_UNKNOWN,
            )

        if not order_id:
            return pay_pb2.PrepareResponse(
                vote_commit=False,
                reason="order_id is required",
                participant_state=pay_pb2.PAYMENT_STATE_UNKNOWN,
            )

        if request.amount < 0:
            return pay_pb2.PrepareResponse(
                vote_commit=False,
                reason="amount cannot be negative",
                participant_state=pay_pb2.PAYMENT_STATE_UNKNOWN,
            )

        if not currency:
            return pay_pb2.PrepareResponse(
                vote_commit=False,
                reason="currency is required",
                participant_state=pay_pb2.PAYMENT_STATE_UNKNOWN,
            )

        with self._lock:
            existing = self._transactions.get(tx_id)
            if existing is None:
                self._transactions[tx_id] = TxState(
                    transaction_id=tx_id,
                    order_id=order_id,
                    amount=request.amount,
                    currency=currency,
                    state=pay_pb2.PAYMENT_STATE_PREPARED,
                    reason="Prepared and ready to commit",
                )
                log.info(
                    "2PC_PREPARE tx_id=%s order_id=%s vote=COMMIT amount=%.2f currency=%s",
                    tx_id,
                    order_id,
                    request.amount,
                    currency,
                )
                self._persist_state()
                return pay_pb2.PrepareResponse(
                    vote_commit=True,
                    reason="Prepared and ready to commit",
                    participant_state=pay_pb2.PAYMENT_STATE_PREPARED,
                )

            if not self._is_same_prepare_payload(existing, order_id, request.amount, currency):
                return pay_pb2.PrepareResponse(
                    vote_commit=False,
                    reason="transaction_id already used with different payload",
                    participant_state=existing.state,
                )

            if existing.state == pay_pb2.PAYMENT_STATE_PREPARED:
                return pay_pb2.PrepareResponse(
                    vote_commit=True,
                    reason="Already prepared",
                    participant_state=pay_pb2.PAYMENT_STATE_PREPARED,
                )

            if existing.state == pay_pb2.PAYMENT_STATE_COMMITTED:
                return pay_pb2.PrepareResponse(
                    vote_commit=True,
                    reason="Already committed",
                    participant_state=pay_pb2.PAYMENT_STATE_COMMITTED,
                )

            return pay_pb2.PrepareResponse(
                vote_commit=False,
                reason="Transaction already aborted",
                participant_state=pay_pb2.PAYMENT_STATE_ABORTED,
            )

    def Commit(self, request, context):
        tx_id = (request.transaction_id or "").strip()
        if not tx_id:
            return pay_pb2.CommitResponse(
                committed=False,
                reason="transaction_id is required",
                payment_id="",
                participant_state=pay_pb2.PAYMENT_STATE_UNKNOWN,
            )

        with self._lock:
            state = self._transactions.get(tx_id)
            if state is None:
                return pay_pb2.CommitResponse(
                    committed=False,
                    reason="Transaction not found; Prepare required before Commit",
                    payment_id="",
                    participant_state=pay_pb2.PAYMENT_STATE_MISSING,
                )

            if state.state == pay_pb2.PAYMENT_STATE_ABORTED:
                return pay_pb2.CommitResponse(
                    committed=False,
                    reason="Transaction already aborted",
                    payment_id="",
                    participant_state=pay_pb2.PAYMENT_STATE_ABORTED,
                )

            if state.state != pay_pb2.PAYMENT_STATE_COMMITTED:
                state.state = pay_pb2.PAYMENT_STATE_COMMITTED
                state.payment_id = state.payment_id or f"pay-{uuid4().hex[:10]}"
                state.reason = "Committed"
                log.info(
                    "2PC_COMMIT tx_id=%s order_id=%s payment_id=%s",
                    tx_id,
                    state.order_id,
                    state.payment_id,
                )
                self._persist_state()

            return pay_pb2.CommitResponse(
                committed=True,
                reason="Committed",
                payment_id=state.payment_id,
                participant_state=pay_pb2.PAYMENT_STATE_COMMITTED,
            )

    def Abort(self, request, context):
        tx_id = (request.transaction_id or "").strip()
        abort_reason = (request.reason or "").strip() or "Aborted by coordinator"

        if not tx_id:
            return pay_pb2.AbortResponse(
                aborted=False,
                reason="transaction_id is required",
                participant_state=pay_pb2.PAYMENT_STATE_UNKNOWN,
            )

        with self._lock:
            state = self._transactions.get(tx_id)
            if state is None:
                self._transactions[tx_id] = TxState(
                    transaction_id=tx_id,
                    order_id="",
                    amount=0.0,
                    currency="",
                    state=pay_pb2.PAYMENT_STATE_ABORTED,
                    reason=abort_reason,
                )
                log.info("2PC_ABORT tx_id=%s reason=%s", tx_id, abort_reason)
                self._persist_state()
                return pay_pb2.AbortResponse(
                    aborted=True,
                    reason=abort_reason,
                    participant_state=pay_pb2.PAYMENT_STATE_ABORTED,
                )

            if state.state == pay_pb2.PAYMENT_STATE_COMMITTED:
                return pay_pb2.AbortResponse(
                    aborted=False,
                    reason="Transaction already committed",
                    participant_state=pay_pb2.PAYMENT_STATE_COMMITTED,
                )

            state.state = pay_pb2.PAYMENT_STATE_ABORTED
            state.reason = abort_reason
            log.info("2PC_ABORT tx_id=%s reason=%s", tx_id, abort_reason)
            self._persist_state()
            return pay_pb2.AbortResponse(
                aborted=True,
                reason=abort_reason,
                participant_state=pay_pb2.PAYMENT_STATE_ABORTED,
            )

    def GetTransactionStatus(self, request, context):
        tx_id = (request.transaction_id or "").strip()
        if not tx_id:
            return pay_pb2.GetTransactionStatusResponse(
                transaction_id="",
                participant_state=pay_pb2.PAYMENT_STATE_UNKNOWN,
                order_id="",
                payment_id="",
                reason="transaction_id is required",
            )

        with self._lock:
            state = self._transactions.get(tx_id)
            if state is None:
                return pay_pb2.GetTransactionStatusResponse(
                    transaction_id=tx_id,
                    participant_state=pay_pb2.PAYMENT_STATE_MISSING,
                    order_id="",
                    payment_id="",
                    reason="Transaction not found",
                )

            return pay_pb2.GetTransactionStatusResponse(
                transaction_id=tx_id,
                participant_state=state.state,
                order_id=state.order_id,
                payment_id=state.payment_id,
                reason=state.reason,
            )


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    pay_grpc.add_PaymentServiceServicer_to_server(PaymentService(), server)

    port = os.getenv("PAYMENT_PORT", "50055")
    server.add_insecure_port("[::]:" + port)
    server.start()
    log.info("=" * 60)
    log.info("Payment Service started on port %s", port)
    log.info("=" * 60)

    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        log.info("Shutting down Payment Service...")
        server.stop(0)


if __name__ == "__main__":
    serve()
