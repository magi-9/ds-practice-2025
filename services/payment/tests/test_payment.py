# pyright: reportMissingImports=false

import os
import sys
from concurrent import futures

import grpc

FILE = __file__
repo_root = os.path.abspath(os.path.join(os.path.dirname(FILE), "../../.."))
sys.path.insert(0, repo_root)
sys.path.insert(0, os.path.join(repo_root, "utils", "pb"))

import payment.payment_pb2 as pay_pb2
import payment.payment_pb2_grpc as pay_grpc

from services.payment.src.app import PaymentService


class TestPaymentService:
    def setup_method(self):
        self.service = PaymentService()
        self.context = None

    def test_prepare_accepts_valid_transaction(self):
        response = self.service.Prepare(
            pay_pb2.PrepareRequest(
                transaction_id="tx-1",
                order_id="order-1",
                amount=25.5,
                currency="EUR",
            ),
            self.context,
        )

        assert response.vote_commit is True
        assert response.participant_state == pay_pb2.PAYMENT_STATE_PREPARED

    def test_prepare_rejects_invalid_request(self):
        response = self.service.Prepare(
            pay_pb2.PrepareRequest(
                transaction_id="",
                order_id="order-1",
                amount=10,
                currency="EUR",
            ),
            self.context,
        )

        assert response.vote_commit is False
        assert response.reason == "transaction_id is required"

    def test_commit_after_prepare_is_successful(self):
        self.service.Prepare(
            pay_pb2.PrepareRequest(
                transaction_id="tx-2",
                order_id="order-2",
                amount=30,
                currency="EUR",
            ),
            self.context,
        )

        response = self.service.Commit(
            pay_pb2.CommitRequest(transaction_id="tx-2"),
            self.context,
        )

        assert response.committed is True
        assert response.participant_state == pay_pb2.PAYMENT_STATE_COMMITTED
        assert response.payment_id != ""

    def test_abort_after_prepare_is_successful(self):
        self.service.Prepare(
            pay_pb2.PrepareRequest(
                transaction_id="tx-3",
                order_id="order-3",
                amount=50,
                currency="EUR",
            ),
            self.context,
        )

        response = self.service.Abort(
            pay_pb2.AbortRequest(transaction_id="tx-3", reason="coordinator timeout"),
            self.context,
        )

        assert response.aborted is True
        assert response.participant_state == pay_pb2.PAYMENT_STATE_ABORTED

    def test_commit_without_prepare_fails(self):
        response = self.service.Commit(
            pay_pb2.CommitRequest(transaction_id="tx-missing"),
            self.context,
        )

        assert response.committed is False
        assert response.participant_state == pay_pb2.PAYMENT_STATE_MISSING

    def test_prepare_is_idempotent(self):
        first = self.service.Prepare(
            pay_pb2.PrepareRequest(
                transaction_id="tx-4",
                order_id="order-4",
                amount=10,
                currency="EUR",
            ),
            self.context,
        )
        second = self.service.Prepare(
            pay_pb2.PrepareRequest(
                transaction_id="tx-4",
                order_id="order-4",
                amount=10,
                currency="EUR",
            ),
            self.context,
        )

        assert first.vote_commit is True
        assert second.vote_commit is True
        assert second.reason == "Already prepared"

    def test_prepare_rejects_reused_tx_id_with_different_payload(self):
        self.service.Prepare(
            pay_pb2.PrepareRequest(
                transaction_id="tx-6",
                order_id="order-6",
                amount=10,
                currency="EUR",
            ),
            self.context,
        )

        second = self.service.Prepare(
            pay_pb2.PrepareRequest(
                transaction_id="tx-6",
                order_id="order-6",
                amount=12,
                currency="EUR",
            ),
            self.context,
        )

        assert second.vote_commit is False
        assert second.reason == "transaction_id already used with different payload"

    def test_status_endpoint_reflects_committed_state(self):
        self.service.Prepare(
            pay_pb2.PrepareRequest(
                transaction_id="tx-5",
                order_id="order-5",
                amount=70,
                currency="EUR",
            ),
            self.context,
        )
        commit = self.service.Commit(pay_pb2.CommitRequest(transaction_id="tx-5"), self.context)

        status = self.service.GetTransactionStatus(
            pay_pb2.GetTransactionStatusRequest(transaction_id="tx-5"),
            self.context,
        )

        assert status.participant_state == pay_pb2.PAYMENT_STATE_COMMITTED
        assert status.payment_id == commit.payment_id


class TestPaymentRecoveryAndGrpcPath:
    def test_state_is_recovered_after_service_restart(self, tmp_path):
        state_file = tmp_path / "payment-state.json"

        first = PaymentService(state_file_path=str(state_file))
        first.Prepare(
            pay_pb2.PrepareRequest(
                transaction_id="tx-r1",
                order_id="order-r1",
                amount=42,
                currency="EUR",
            ),
            None,
        )
        commit = first.Commit(pay_pb2.CommitRequest(transaction_id="tx-r1"), None)

        second = PaymentService(state_file_path=str(state_file))
        status = second.GetTransactionStatus(
            pay_pb2.GetTransactionStatusRequest(transaction_id="tx-r1"),
            None,
        )

        assert status.participant_state == pay_pb2.PAYMENT_STATE_COMMITTED
        assert status.payment_id == commit.payment_id

    def test_grpc_prepare_commit_smoke(self, tmp_path):
        state_file = tmp_path / "payment-grpc-state.json"
        server = grpc.server(futures.ThreadPoolExecutor(max_workers=2))
        pay_grpc.add_PaymentServiceServicer_to_server(PaymentService(state_file_path=str(state_file)), server)
        port = server.add_insecure_port("127.0.0.1:0")
        server.start()

        try:
            channel = grpc.insecure_channel(f"127.0.0.1:{port}")
            stub = pay_grpc.PaymentServiceStub(channel)

            prepared = stub.Prepare(
                pay_pb2.PrepareRequest(
                    transaction_id="tx-grpc-1",
                    order_id="order-grpc-1",
                    amount=15,
                    currency="EUR",
                )
            )
            committed = stub.Commit(pay_pb2.CommitRequest(transaction_id="tx-grpc-1"))

            assert prepared.vote_commit is True
            assert committed.committed is True
            assert committed.payment_id != ""
        finally:
            channel.close()
            server.stop(0)
