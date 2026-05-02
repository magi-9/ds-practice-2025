import sys
import os
import logging
import time
from uuid import uuid4
import json

import grpc

FILE = __file__ if "__file__" in globals() else os.getenv("PYTHONFILE", "")
pb_root = os.path.abspath(os.path.join(FILE, "../../../../utils/pb"))
sys.path.insert(0, pb_root)

import order_queue.order_queue_pb2 as oq_pb2
import order_queue.order_queue_pb2_grpc as oq_grpc
import payment.payment_pb2 as pay_pb2
import payment.payment_pb2_grpc as pay_grpc
import books_database.books_database_pb2 as db_pb2
import books_database.books_database_pb2_grpc as db_grpc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("order_executor")

DB_HOST = os.getenv("DB_HOST", "books_database_primary:50056")
QUEUE_HOST = os.getenv("QUEUE_HOST", "order_queue:50054")
PAYMENT_HOST = os.getenv("PAYMENT_HOST", "payment:50055")
EXECUTOR_ID = os.getenv("EXECUTOR_ID", f"executor-{uuid4().hex[:8]}")


def safe_abort_payment(payment_stub, tx_id, reason):
    try:
        payment_stub.Abort(pay_pb2.AbortRequest(transaction_id=tx_id, reason=reason), timeout=3)
    except Exception:
        pass


def safe_abort_db(db_stub, tx_id, reason):
    try:
        db_stub.Abort(db_pb2.AbortRequest(transaction_id=tx_id, reason=reason), timeout=3)
    except Exception:
        pass


class ExecutorService:
    def __init__(self, executor_id, queue_stub, payment_stub, db_stub):
        self.executor_id = executor_id
        self.queue_stub = queue_stub
        self.payment_stub = payment_stub
        self.db_stub = db_stub
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

    def execute_2pc(self, order_id, response, tx_id, items):
        """Execute 2-Phase Commit protocol with payment and database"""
        amount = 1.0
        currency = "EUR"
        
        log.info("2PC START tx=%s order=%s with %d items", tx_id, order_id, len(items))
        
        # === PHASE 1: PREPARE ===
        log.info("2PC PHASE 1 (Prepare) tx=%s", tx_id)
        
        # Prepare payment
        try:
            pay_prep = self.payment_stub.Prepare(
                pay_pb2.PrepareRequest(
                    transaction_id=tx_id,
                    order_id=order_id,
                    amount=amount,
                    currency=currency,
                ),
                timeout=3,
            )
            if not pay_prep.vote_commit:
                log.info("2PC PREPARE vote=NO (payment) tx=%s reason=%s", tx_id, pay_prep.reason)
                safe_abort_payment(self.payment_stub, tx_id, "prepare vote=no")
                return False
            log.info("2PC PREPARE vote=YES (payment) tx=%s", tx_id)
        except Exception as e:
            log.warning("2PC PREPARE error (payment) tx=%s err=%s", tx_id, e)
            safe_abort_payment(self.payment_stub, tx_id, "prepare timeout/error")
            return False
        
        # Prepare database updates
        db_prepares = []
        for item in items:
            title = item.get("name", "")
            quantity = item.get("quantity", 1)
            
            # Read current stock
            try:
                read_resp = self.db_stub.Read(db_pb2.ReadRequest(title=title), timeout=3)
                if not read_resp.found:
                    log.warning("Book not found: title=%s", title)
                    safe_abort_payment(self.payment_stub, tx_id, f"book not found: {title}")
                    for p in db_prepares:
                        safe_abort_db(self.db_stub, p["tx_id"], "item not found")
                    return False
                
                new_stock = max(0, read_resp.stock - quantity)
                item_tx_id = f"{tx_id}_{title}"
                
                # Prepare DB write
                try:
                    db_prep = self.db_stub.Prepare(
                        db_pb2.PrepareRequest(
                            transaction_id=item_tx_id,
                            title=title,
                            new_stock=new_stock,
                        ),
                        timeout=3,
                    )
                    if not db_prep.vote_commit:
                        log.info("2PC PREPARE vote=NO (db) tx=%s title=%s reason=%s", tx_id, title, db_prep.reason)
                        safe_abort_payment(self.payment_stub, tx_id, f"db prepare no: {title}")
                        for p in db_prepares:
                            safe_abort_db(self.db_stub, p["tx_id"], "other item prepare failed")
                        return False
                    
                    log.info("2PC PREPARE vote=YES (db) tx=%s title=%s old=%d new=%d", 
                             tx_id, title, read_resp.stock, new_stock)
                    db_prepares.append({
                        "tx_id": item_tx_id,
                        "title": title,
                        "old_stock": read_resp.stock,
                        "new_stock": new_stock,
                    })
                except Exception as e:
                    log.warning("2PC PREPARE error (db) tx=%s title=%s err=%s", tx_id, title, e)
                    safe_abort_payment(self.payment_stub, tx_id, f"db prepare timeout: {title}")
                    for p in db_prepares:
                        safe_abort_db(self.db_stub, p["tx_id"], "prepare timeout")
                    return False
            
            except Exception as e:
                log.warning("2PC PREPARE read error tx=%s title=%s err=%s", tx_id, title, e)
                safe_abort_payment(self.payment_stub, tx_id, f"db read error: {title}")
                return False
        
        # === PHASE 2: COMMIT ===
        log.info("2PC PHASE 2 (Commit) tx=%s with %d db items", tx_id, len(db_prepares))
        
        commit_success = True
        
        # Commit payment
        try:
            pay_com = self.payment_stub.Commit(
                pay_pb2.CommitRequest(transaction_id=tx_id),
                timeout=3
            )
            if pay_com.committed:
                log.info("2PC COMMIT success (payment) tx=%s payment_id=%s", tx_id, pay_com.payment_id)
            else:
                log.error("2PC COMMIT failed (payment) tx=%s reason=%s", tx_id, pay_com.reason)
                commit_success = False
        except Exception as e:
            log.warning("2PC COMMIT error (payment) tx=%s err=%s", tx_id, e)
            commit_success = False
        
        # Commit database updates
        for prep in db_prepares:
            try:
                db_com = self.db_stub.Commit(
                    db_pb2.CommitRequest(transaction_id=prep["tx_id"]),
                    timeout=3
                )
                if db_com.committed:
                    log.info("2PC COMMIT success (db) tx=%s title=%s %d -> %d",
                             tx_id, prep["title"], prep["old_stock"], prep["new_stock"])
                else:
                    log.error("2PC COMMIT failed (db) tx=%s title=%s reason=%s",
                              tx_id, prep["title"], db_com.reason)
                    commit_success = False
            except Exception as e:
                log.warning("2PC COMMIT error (db) tx=%s title=%s err=%s", tx_id, prep["title"], e)
                commit_success = False
        
        if commit_success:
            log.info("2PC SUCCESS: order=%s tx=%s executed with %d items", order_id, tx_id, len(db_prepares))
        else:
            log.error("2PC PARTIAL FAILURE: order=%s tx=%s (some commits failed)", order_id, tx_id)
        
        return commit_success

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

                        # Parse order items
                        try:
                            order_json = json.loads(response.order.order_json)
                            items = order_json.get("items", [])
                        except Exception as e:
                            log.error("Failed to parse order JSON order=%s err=%s", order_id, e)
                            time.sleep(3)
                            continue

                        # Execute 2PC
                        result = self.execute_2pc(order_id, response, tx_id, items)
                        log.info(
                            "2PC END tx=%s order=%s outcome=%s",
                            tx_id,
                            order_id,
                            "COMMIT" if result else "ABORT",
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

    db_channel = grpc.insecure_channel(DB_HOST)
    db_stub = db_grpc.BooksDatabaseStub(db_channel)

    svc = ExecutorService(EXECUTOR_ID, queue_stub, payment_stub, db_stub)
    svc.start_leader_election()
    svc.run()


if __name__ == "__main__":
    launch_executor()
