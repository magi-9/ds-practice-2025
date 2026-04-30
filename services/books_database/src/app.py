import sys
import os
import logging
import time
from threading import RLock

import grpc
from concurrent import futures

# This set of lines are needed to import the gRPC stubs.
# The path of the stubs is relative to the current file, or absolute inside the container.
# Change these lines only if strictly needed.
FILE = __file__ if '__file__' in globals() else os.getenv("PYTHONFILE", "")
pb_root = os.path.abspath(os.path.join(FILE, "../../../../utils/pb"))
sys.path.insert(0, pb_root)

import books_database.books_database_pb2 as books_pb2
import books_database.books_database_pb2_grpc as books_grpc

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("books_database")

starting_stock = {
    "The Great Gatsby": 1000,
    "1984": 1984,
}

class BooksDatabaseServicer(books_grpc.BooksDatabaseServicer):
    def __init__(self, role):
        self.store = dict(starting_stock)
        self._lock = RLock()
        self.role = role

    def Read(self, request, context):
        if self.role != "primary":
            return books_pb2.ReadResponse(stock=0, found=False, reason="Reads must go to primary")
        
        with self._lock:
            stock = self.store.get(request.title, 0)
            found = request.title in self.store
            log.info("Read title=%s", request.title)
            return books_pb2.ReadResponse(stock=stock, found=found, reason="OK" if found else "Not found")

    def Write(self, request, context):
        if self.role != "backup":
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "Backup is read only")
            return

        with self._lock:
            self.store[request.title] = request.new_stock
            log.info("backup write title=%s", request.title)
        return books_pb2.WriteResponse(success=True, reason="OK")

class PrimaryReplica(BooksDatabaseServicer):
    def __init__(self, backup_stubs):
        super().__init__(role="primary")
        self.backups = backup_stubs

    
    def Write(self, request, context):
        with self._lock:
            old_value = self.store.get(request.title)
            self.store[request.title] = request.new_stock
            log.info("Primary applied local write: title=%s old=%s new=%d", request.title, old_value, request.new_stock)

        quorum = 1  # assuming that primary aways succeeds
        #majority quorum
        total = len(self.backups) + 1
        quorum_needed = total // 2 + 1

        log.info("Starting replication")
        for i, backup in enumerate(self.backups):
            try:
                resp = backup.Write(request, timeout=3)
                if resp.success:
                    quorum += 1
                    log.info("Backup %d confirmed write", i)
            except Exception:
                log.warning("backup %d failed write", i)
                pass

        if quorum < quorum_needed:
            log.error("Quorum failed quorum=%d required=%d, rolling back", quorum, quorum_needed)
            with self._lock:
                if old_value is None:
                    self.store.pop(request.title, None)
                    log.info("Rollback is complete and title=%s removed", request.title)
                else:
                    log.info("Rollback complete: title=%s restored to %d", request.title, old_value)
                    self.store[request.title] = old_value
            return books_pb2.WriteResponse(success=False, reason="quorum not reached")

        log.info("Write successfully committed! title=%s", request.title)
        return books_pb2.WriteResponse(success=True, reason="OK")

def serve():
    port = os.getenv("DB_PORT", "50056")
    role = os.getenv("DB_ROLE", "backup")
    if role == "primary":
        backup_stubs = []
        for host in [h.strip() for h in os.getenv("BACKUP_HOSTS", "").split(",") if h]:
            channel = grpc.insecure_channel(host)
            stub = books_grpc.BooksDatabaseStub(channel)
            backup_stubs.append(stub)
            log.info("Connected to backup %s", host)
        servicer = PrimaryReplica(backup_stubs)
    else:
        servicer = BooksDatabaseServicer(role="backup")

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    books_grpc.add_BooksDatabaseServicer_to_server(servicer, server)

    server.add_insecure_port("[::]:" + port)
    server.start()
    log.info("BOOKS DATABASE started! role=%s", role)
    server.wait_for_termination()

if __name__ == "__main__":
    serve()