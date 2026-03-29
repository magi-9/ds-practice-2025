import os
import sys
import json
import logging
import time
from collections import deque
from threading import RLock
from uuid import uuid4
from datetime import datetime

import grpc
from concurrent import futures

# Import proto
pb_root = os.path.dirname(__file__)
sys.path.insert(0, pb_root)

import order_queue.order_queue_pb2 as oq_pb2
import order_queue.order_queue_pb2_grpc as oq_grpc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("order_queue")


class OrderQueueService(oq_grpc.OrderQueueServiceServicer):
    def __init__(self):
        self._queue: deque = deque()
        self._lock = RLock()
        self._dequeue_count = 0
        self._leader_id = ""
        self._leader_token = ""

    def RegisterExecutor(self, request, context):
        try:
            with self._lock:
                # first executor becomes leader
                if not self._leader_id:
                    self._leader_id = request.executor_id
                    self._leader_token = uuid4().hex
                    log.info("LEADER ELECTED: leader_id=%s", self._leader_id)

                    return oq_pb2.RegisterExecutorResponse(
                        success=True,
                        leader_id=self._leader_id,
                        executor_token=self._leader_token,
                        reason="You are leader"
                    )

                # others are not leader
                return oq_pb2.RegisterExecutorResponse(
                    success=True,
                    leader_id=self._leader_id,
                    executor_token="",
                    reason="Leader already elected"
                )

        except Exception as e:
            log.error("RegisterExecutor ERROR: %s", e)
            return oq_pb2.RegisterExecutorResponse(
                success=False,
                leader_id=self._leader_id,
                executor_token="",
                reason=str(e)
            )

    def GetLeader(self, request, context):
        with self._lock:
            return oq_pb2.GetLeaderResponse(leader_id=self._leader_id)

    def Dequeue(self, request, context):
        try:
            with self._lock:
                # no leader yet
                if not self._leader_token:
                    return oq_pb2.DequeueResponse(
                        success=False,
                        order=None,
                        dequeue_id="",
                        reason="No leader elected yet"
                    )

                # only leader allowed
                if (request.executor_token != self._leader_token) or (request.executor_id != self._leader_id):
                    log.warning(
                        "DEQUEUE REJECTED: not leader executor_id=%s leader_id=%s",
                        request.executor_id,
                        self._leader_id
                    )
                    return oq_pb2.DequeueResponse(
                        success=False,
                        order=None,
                        dequeue_id="",
                        reason="Not leader"
                    )

                if len(self._queue) == 0:
                    return oq_pb2.DequeueResponse(
                        success=False,
                        order=None,
                        dequeue_id="",
                        reason="Queue is empty"
                    )

                order = self._queue.popleft()
                self._dequeue_count += 1
                dequeue_id = f"d-{self._dequeue_count}-{uuid4().hex[:8]}"

            log.info(
                "DEQUEUE: executor_id=%s order_id=%s dequeue_id=%s remaining=%d",
                request.executor_id,
                order.order_id,
                dequeue_id,
                len(self._queue)
            )

            return oq_pb2.DequeueResponse(
                success=True,
                order=order,
                dequeue_id=dequeue_id,
                reason="Order dequeued successfully"
            )

        except Exception as e:
            log.error("DEQUEUE ERROR: executor_id=%s error=%s", request.executor_id, e)
            return oq_pb2.DequeueResponse(
                success=False,
                order=None,
                dequeue_id="",
                reason=f"Dequeue failed: {str(e)}"
            )
    
    
    
    def Dequeue(self, request, context):
        """Remove and return front order - thread-safe, FIFO"""
        try:
            with self._lock:

                if request.executor_token != self._leader_token:
                    log.warning(
                        "DEQUEUE REJECTED: not leader executor_id=%s leader_id=%s",
                        request.executor_id,
                        self._leader_id
                    )
                    return oq_pb2.DequeueResponse(
                        success=False,
                        order=None,
                        dequeue_id="",
                        reason="Not leader"
                    )

                if len(self._queue) == 0:
                    log.warning("DEQUEUE: Queue empty, executor_id=%s", request.executor_id)
                    return oq_pb2.DequeueResponse(
                        success=False,
                        order=None,
                        dequeue_id="",
                        reason="Queue is empty"
                    )
                
                # Pop from front (FIFO)
                order = self._queue.popleft()
                self._dequeue_count += 1
                dequeue_id = f"d-{self._dequeue_count}-{uuid4().hex[:8]}"
                remaining = len(self._queue)
            
            log.info(
                "DEQUEUE: executor_id=%s order_id=%s dequeue_id=%s remaining=%d",
                request.executor_id,
                order.order_id,
                dequeue_id,
                remaining
            )
            
            return oq_pb2.DequeueResponse(
                success=True,
                order=order,
                dequeue_id=dequeue_id,
                reason="Order dequeued successfully"
            )
        
        except Exception as e:
            log.error("DEQUEUE ERROR: executor_id=%s error=%s", request.executor_id, e)
            return oq_pb2.DequeueResponse(
                success=False,
                order=None,
                dequeue_id="",
                reason=f"Dequeue failed: {str(e)}"
            )
    
    def GetQueueSize(self, request, context):
        """Return current queue size"""
        with self._lock:
            size = len(self._queue)
        
        log.debug("GetQueueSize: size=%d", size)
        return oq_pb2.GetQueueSizeResponse(queue_size=size)
    
    def PeekOrder(self, request, context):
        """Look at order without removing (diagnostics only)"""
        try:
            with self._lock:
                if request.position >= len(self._queue) or request.position < 0:
                    return oq_pb2.PeekOrderResponse(
                        order=None,
                        exists=False
                    )
                
                order = self._queue[request.position]
            
            log.debug("PeekOrder: position=%d order_id=%s", request.position, order.order_id)
            return oq_pb2.PeekOrderResponse(
                order=order,
                exists=True
            )
        
        except Exception as e:
            log.error("PeekOrder error: %s", e)
            return oq_pb2.PeekOrderResponse(
                order=None,
                exists=False
            )

def RegisterExecutor(self, request, context):
    try:
        with self._lock:
            if not self._leader_id:
                self._leader_id = request.executor_id
                self._leader_token = uuid4().hex
                log.info("LEADER ELECTED: leader_id=%s", self._leader_id)

                return oq_pb2.RegisterExecutorResponse(
                    success=True,
                    leader_id=self._leader_id,
                    executor_token=self._leader_token,
                    reason="You are leader"
                )

            return oq_pb2.RegisterExecutorResponse(
                success=True,
                leader_id=self._leader_id,
                executor_token="",
                reason="Leader already elected"
            )
    except Exception as e:
        log.error("RegisterExecutor ERROR: %s", e)
        return oq_pb2.RegisterExecutorResponse(
            success=False,
            leader_id=self._leader_id,
            executor_token="",
            reason=str(e)
        )

def GetLeader(self, request, context):
    with self._lock:
        leader = self._leader_id
    return oq_pb2.GetLeaderResponse(leader_id=leader)

def serve():
    """Start the Order Queue gRPC service"""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    oq_grpc.add_OrderQueueServiceServicer_to_server(OrderQueueService(), server)
    
    port = "50054"
    server.add_insecure_port("[::]:" + port)
    server.start()
    log.info("=" * 60)
    log.info("Order Queue Service started on port %s", port)
    log.info("=" * 60)
    
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        log.info("Shutting down Order Queue Service...")
        server.stop(0)


if __name__ == "__main__":
    serve()
