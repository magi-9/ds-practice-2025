import sys
import os
import logging
import time
from uuid import uuid4

FILE = __file__ if '__file__' in globals() else os.getenv("PYTHONFILE", "")
pb_root = os.path.abspath(os.path.join(FILE, "../../../../utils/pb"))
sys.path.insert(0, pb_root)

import order_queue.order_queue_pb2 as oq_pb2
import order_queue.order_queue_pb2_grpc as oq_grpc

import grpc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("order_executor")

QUEUE_HOST = os.getenv("QUEUE_HOST", "order_queue:50054")
EXECUTOR_ID = os.getenv("EXECUTOR_ID", f"executor-{uuid4().hex[:8]}")

class ExecutorService:
    def __init__(self, executor_id, queue_stub):
        self.executor_id = executor_id
        self.queue_stub = queue_stub
        self.leader_id = None
        self.executor_token = None

    def start_leader_election(self):
        #register with queue and find out if we are leader
        while True:
            try:
                response = self.queue_stub.RegisterExecutor(
                    oq_pb2.RegisterExecutorRequest(executor_id=self.executor_id),
                    timeout=3)
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
        #if leader, repeatedly dequeue and execute orders else wait
        is_leader = self.leader_id == self.executor_id
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
                        log.info(
                            "Order is being executed: order_id=%s dequeue_id=%s",
                            response.order.order_id,
                            response.dequeue_id,
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
    channel = grpc.insecure_channel(QUEUE_HOST)
    queue_stub = oq_grpc.OrderQueueServiceStub(channel)

    svc = ExecutorService(EXECUTOR_ID, queue_stub)
    svc.start_leader_election()
    svc.run()

if __name__ == "__main__":
    launch_executor()