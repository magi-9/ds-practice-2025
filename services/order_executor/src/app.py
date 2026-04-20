import sys
import os
import logging
import time
from uuid import uuid4

import grpc

FILE = __file__ if "__file__" in globals() else os.getenv("PYTHONFILE", "")
pb_root = os.path.abspath(os.path.join(FILE, "../../../../utils/pb"))
sys.path.insert(0, pb_root)

import order_queue.order_queue_pb2 as oq_pb2
import order_queue.order_queue_pb2_grpc as oq_grpc
import payment.payment_pb2 as pay_pb2
import payment.payment_pb2_grpc as pay_grpc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("order_executor")

QUEUE_HOST = os.getenv("QUEUE_HOST", "order_queue:50054")
PAYMENT_HOST = os.getenv("PAYMENT_HOST", "payment:50055")
EXECUTOR_ID = os.getenv("EXECUTOR_ID", f"executor-{uuid4().hex[:8]}")


def safe_abort(payment_stub, tx_id, reason):
    try:
        payment_stub.Abort(pay_pb2.AbortRequest(transaction_id=tx_id, reason=reason), timeout=3)
    except Exception:
        pass


class ExecutorService:
    def __init__(self, executor_id, queue_stub, payment_stub):
        self.executor_id = executor_id
        self.queue_stub = queue_stub
        self.payment_stub = payment_stub
        self.leader_id = None
        self.executor_token = None

    def start_leader_election(self):
        # register with queue and find out who is leader
        while True:
            try:
                response = self.queue_stub.RegisterExecutor(
                    oq_pb2.RegisterExecutorRequest(executor_id=self.executor_id),
                    timeout=3
                )
                self.leader_id = response.leader_id
                self.executor_token = response.executor_token
                log.info(
                    "Registered: executor_id=%s leader_id=%s reason=%s",
                    self.executor_id,
                    self.leader_id,
                    response.reason,
                )
                return
            except Exception as e:
                log.warning("Queue is not ready error=%s", e)
                time.sleep(3)

    def run(self):
        is_leader = (self.leader_id == self.executor_id)
        if is_leader:
            log.info("I'm the leader, I will repeatedly dequeue and 'execute' orders.")
        else:
            log.info("I'm not the leader, leader is %s", self.leader_id)

        idle_ticks = 0
        while True:
            if is_leader:
                try:
                    response = self.queue_stub.Dequeue(
                        oq_pb2.DequeueRequest(
                            executor_id=self.executor_id,
                            executor_token=self.executor_token,
                        ),
                        timeout=3
                    )

                    if response.success:
                        order_id = response.order.order_id
                        tx_id = uuid4().hex[:12]

                        amount = 1.0
                        currency = "EUR"

                        log.info("2PC START tx=%s order=%s", tx_id, order_id)

                        # phase 1 : repair (payment)
                        try:
                            prep = self.payment_stub.Prepare(
                                pay_pb2.PrepareRequest(
                                    transaction_id=tx_id,
                                    order_id=order_id,
                                    amount=amount,
                                    currency=currency,
                                ),
                                timeout=3,
                            )
                        except Exception as e:
                            log.warning("2PC PREPARE error tx=%s err=%s => ABORT", tx_id, e)
                            safe_abort(self.payment_stub, tx_id, "prepare timeout/error")
                            time.sleep(3)
                            continue

                        if not prep.vote_commit:
                            log.info("2PC PREPARE vote=ABORT tx=%s reason=%s", tx_id, prep.reason)
                            safe_abort(self.payment_stub, tx_id, prep.reason)
                            time.sleep(3)
                            continue

                        log.info("2PC PREPARE vote=COMMIT tx=%s", tx_id)

                        # phase 2: commit (payment)
                        try:
                            com = self.payment_stub.Commit(
                                pay_pb2.CommitRequest(transaction_id=tx_id),
                                timeout=3
                            )
                            log.info(
                                "2PC COMMIT tx=%s committed=%s payment_id=%s",
                                tx_id,
                                com.committed,
                                com.payment_id,
                            )
                        except Exception as e:
                            log.warning("2PC COMMIT uncertain tx=%s err=%s", tx_id, e)

                        log.info(
                            "Order is being executed: order_id=%s dequeue_id=%s tx=%s",
                            order_id,
                            response.dequeue_id,
                            tx_id,
                        )
                        idle_ticks = 0
                    else:
                        idle_ticks += 1
                        if idle_ticks % 10 == 1:
                            log.info("Waiting for orders... (queue empty)")

                except Exception as e:
                    log.error("dequeue error: %s", e)
            else:
                idle_ticks += 1
                if idle_ticks % 10 == 1:
                    log.info("Follower waiting: leader is %s", self.leader_id)

            time.sleep(3)


def launch_executor():
    log.info("Order executor is starting and executor_id=%s", EXECUTOR_ID)

    queue_channel = grpc.insecure_channel(QUEUE_HOST)
    payment_channel = grpc.insecure_channel(PAYMENT_HOST)

    queue_stub = oq_grpc.OrderQueueServiceStub(queue_channel)
    payment_stub = pay_grpc.PaymentServiceStub(payment_channel)

    svc = ExecutorService(EXECUTOR_ID, queue_stub, payment_stub)
    svc.start_leader_election()
    svc.run()


if __name__ == "__main__":
    launch_executor()