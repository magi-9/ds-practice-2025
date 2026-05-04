import sys
import os
import logging
import time
from threading import RLock
from dataclasses import dataclass

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

@dataclass
class TxState:
    transaction_id: str
    title: str
    new_stock: int
    old_stock: int
    state: int
    reason: str = ""

class BooksDatabaseServicer(books_grpc.BooksDatabaseServicer):
    def __init__(self, role):
        self.store = dict(starting_stock)
        self._lock = RLock()
        self.role = role
        self._transactions = {}  # transaction_id -> TxState

    def Read(self, request, context):
        if self.role != "primary":
            return books_pb2.ReadResponse(stock=0, found=False, reason="Reads must go to primary")
        
        with self._lock:
            stock = self.store.get(request.title, 0)
            found = request.title in self.store
            log.info("READ title=%s found=%s stock=%d", request.title, found, stock)
            return books_pb2.ReadResponse(stock=stock, found=found, reason="OK" if found else "Not found")

    def Write(self, request, context):
        if self.role != "backup":
            context.abort(grpc.StatusCode.PERMISSION_DENIED, "Backup is read only")
            return

        with self._lock:
            old_stock = self.store.get(request.title)
            self.store[request.title] = request.new_stock
            log.info("BACKUP_WRITE title=%s old_stock=%s new_stock=%d", request.title, old_stock, request.new_stock)
        return books_pb2.WriteResponse(success=True, reason="OK")

    def Prepare(self, request, context):
        """2PC Prepare phase: stage the write"""
        tx_id = (request.transaction_id or "").strip()
        title = (request.title or "").strip()

        if not tx_id:
            return books_pb2.PrepareResponse(
                vote_commit=False,
                reason="transaction_id is required",
                participant_state=books_pb2.DB_STATE_UNKNOWN,
            )

        if not title:
            return books_pb2.PrepareResponse(
                vote_commit=False,
                reason="title is required",
                participant_state=books_pb2.DB_STATE_UNKNOWN,
            )

        with self._lock:
            existing = self._transactions.get(tx_id)
            if existing is None:
                old_stock = self.store.get(title, 0)
                self._transactions[tx_id] = TxState(
                    transaction_id=tx_id,
                    title=title,
                    new_stock=request.new_stock,
                    old_stock=old_stock,
                    state=books_pb2.DB_STATE_PREPARED,
                    reason="Prepared and ready to commit",
                )
                log.info(
                    "2PC_PREPARE tx_id=%s title=%s old_stock=%d new_stock=%d vote=COMMIT state=PREPARED",
                    tx_id,
                    title,
                    old_stock,
                    request.new_stock,
                )
                return books_pb2.PrepareResponse(
                    vote_commit=True,
                    reason="Prepared and ready to commit",
                    participant_state=books_pb2.DB_STATE_PREPARED,
                )

            if existing.state == books_pb2.DB_STATE_PREPARED:
                if existing.title == title and existing.new_stock == request.new_stock:
                    log.info("2PC_PREPARE tx_id=%s title=%s vote=YES state=ALREADY_PREPARED", tx_id, title)
                    return books_pb2.PrepareResponse(
                        vote_commit=True,
                        reason="Already prepared",
                        participant_state=books_pb2.DB_STATE_PREPARED,
                    )
                log.warning("2PC_PREPARE tx_id=%s title=%s vote=NO reason=different_payload", tx_id, title)
                return books_pb2.PrepareResponse(
                    vote_commit=False,
                    reason="transaction_id already used with different payload",
                    participant_state=existing.state,
                )

            if existing.state == books_pb2.DB_STATE_COMMITTED:
                log.info("2PC_PREPARE tx_id=%s title=%s vote=NO state=COMMITTED", tx_id, title)
                return books_pb2.PrepareResponse(
                    vote_commit=False,
                    reason="Transaction already committed",
                    participant_state=books_pb2.DB_STATE_COMMITTED,
                )

            if existing.state == books_pb2.DB_STATE_ABORTED:
                log.info("2PC_PREPARE tx_id=%s title=%s vote=NO state=ABORTED", tx_id, title)
                return books_pb2.PrepareResponse(
                    vote_commit=False,
                    reason="Transaction already aborted",
                    participant_state=books_pb2.DB_STATE_ABORTED,
                )

            return books_pb2.PrepareResponse(
                vote_commit=False,
                reason="Transaction in unknown state",
                participant_state=existing.state,
            )

    def Commit(self, request, context):
        """2PC Commit phase: apply the staged write"""
        tx_id = (request.transaction_id or "").strip()

        if not tx_id:
            return books_pb2.CommitResponse(
                committed=False,
                reason="transaction_id is required",
                participant_state=books_pb2.DB_STATE_UNKNOWN,
            )

        with self._lock:
            state = self._transactions.get(tx_id)
            if state is None:
                self._transactions[tx_id] = TxState(
                    transaction_id=tx_id,
                    title="",
                    new_stock=0,
                    old_stock=0,
                    state=books_pb2.DB_STATE_MISSING,
                    reason="Commit without prepare",
                )
                log.warning("2PC_COMMIT tx_id=%s MISSING (no prepare)", tx_id)
                return books_pb2.CommitResponse(
                    committed=False,
                    reason="Transaction not found",
                    participant_state=books_pb2.DB_STATE_MISSING,
                )

            if state.state != books_pb2.DB_STATE_PREPARED:
                log.warning("2PC_COMMIT tx_id=%s title=%s state=%d invalid", tx_id, state.title, state.state)
                return books_pb2.CommitResponse(
                    committed=False,
                    reason=f"Transaction not in prepared state (state={state.state})",
                    participant_state=state.state,
                )

            # Apply the staged write
            self.store[state.title] = state.new_stock
            state.state = books_pb2.DB_STATE_COMMITTED
            state.reason = "Committed"

            log.info(
                "2PC_COMMIT tx_id=%s title=%s applied old_stock=%d new_stock=%d",
                tx_id,
                state.title,
                state.old_stock,
                state.new_stock,
            )

            return books_pb2.CommitResponse(
                committed=True,
                reason="Committed",
                participant_state=books_pb2.DB_STATE_COMMITTED,
            )

    def Abort(self, request, context):
        """2PC Abort phase: discard the staged write"""
        tx_id = (request.transaction_id or "").strip()
        abort_reason = (request.reason or "").strip() or "Aborted by coordinator"

        if not tx_id:
            return books_pb2.AbortResponse(
                aborted=False,
                reason="transaction_id is required",
                participant_state=books_pb2.DB_STATE_UNKNOWN,
            )

        with self._lock:
            state = self._transactions.get(tx_id)
            if state is None:
                self._transactions[tx_id] = TxState(
                    transaction_id=tx_id,
                    title="",
                    new_stock=0,
                    old_stock=0,
                    state=books_pb2.DB_STATE_ABORTED,
                    reason=abort_reason,
                )
                log.info("2PC_ABORT tx_id=%s reason=%s (no prepare)", tx_id, abort_reason)
                return books_pb2.AbortResponse(
                    aborted=True,
                    reason=abort_reason,
                    participant_state=books_pb2.DB_STATE_ABORTED,
                )

            if state.state == books_pb2.DB_STATE_COMMITTED:
                log.warning("2PC_ABORT tx_id=%s already committed", tx_id)
                return books_pb2.AbortResponse(
                    aborted=False,
                    reason="Transaction already committed",
                    participant_state=books_pb2.DB_STATE_COMMITTED,
                )

            state.state = books_pb2.DB_STATE_ABORTED
            state.reason = abort_reason

            log.info("2PC_ABORT tx_id=%s title=%s reason=%s old_stock=%d new_stock=%d", tx_id, state.title, abort_reason, state.old_stock, state.new_stock)

            return books_pb2.AbortResponse(
                aborted=True,
                reason=abort_reason,
                participant_state=books_pb2.DB_STATE_ABORTED,
            )

    def GetTransactionStatus(self, request, context):
        """Diagnostics: get transaction status"""
        tx_id = (request.transaction_id or "").strip()

        if not tx_id:
            return books_pb2.GetTransactionStatusResponse(
                transaction_id="",
                participant_state=books_pb2.DB_STATE_UNKNOWN,
                reason="transaction_id is required",
            )

        with self._lock:
            state = self._transactions.get(tx_id)
            if state is None:
                return books_pb2.GetTransactionStatusResponse(
                    transaction_id=tx_id,
                    participant_state=books_pb2.DB_STATE_MISSING,
                    reason="Transaction not found",
                )

            return books_pb2.GetTransactionStatusResponse(
                transaction_id=tx_id,
                participant_state=state.state,
                reason=state.reason,
            )

class PrimaryReplica(BooksDatabaseServicer):
    def __init__(self, backup_stubs):
        super().__init__(role="primary")
        self.backups = backup_stubs

    def Prepare(self, request, context):
        """2PC Prepare: stage write on primary, replicate to backups"""
        tx_id = (request.transaction_id or "").strip()
        
        # First, call parent Prepare to stage on primary
        prep_resp = super().Prepare(request, context)
        if not prep_resp.vote_commit:
            return prep_resp

        # Replicate Prepare to backups
        quorum = 1  # primary always succeeds
        total = len(self.backups) + 1
        quorum_needed = total // 2 + 1

        log.info("2PC_PREPARE replicating to %d backups tx_id=%s", len(self.backups), tx_id)
        log.info("2PC_PREPARE quorum threshold tx_id=%s required=%d", tx_id, quorum_needed)
        
        for i, backup in enumerate(self.backups):
            try:
                resp = backup.Prepare(request, timeout=3)
                if resp.vote_commit:
                    quorum += 1
                    log.info("2PC_PREPARE Backup %d confirmed prepare tx_id=%s", i, tx_id)
            except Exception as e:
                log.warning("2PC_PREPARE backup %d failed tx_id=%s err=%s", i, tx_id, e)
                pass

        if quorum < quorum_needed:
            log.error("2PC_PREPARE quorum failed quorum=%d required=%d tx_id=%s, aborting", quorum, quorum_needed, tx_id)
            # Abort on all backups
            for i, backup in enumerate(self.backups):
                try:
                    backup.Abort(books_pb2.AbortRequest(transaction_id=tx_id, reason="quorum not reached"), timeout=3)
                except Exception:
                    pass
            # Abort on primary
            with self._lock:
                state = self._transactions.get(tx_id)
                if state:
                    state.state = books_pb2.DB_STATE_ABORTED
                    state.reason = "quorum not reached"
            return books_pb2.PrepareResponse(
                vote_commit=False,
                reason="quorum not reached during replication",
                participant_state=books_pb2.DB_STATE_ABORTED,
            )

        return prep_resp

    def Commit(self, request, context):
        """2PC Commit: apply on primary, replicate to backups"""
        tx_id = (request.transaction_id or "").strip()
        
        # First, call parent Commit to apply on primary
        com_resp = super().Commit(request, context)
        
        if com_resp.committed:
            # Replicate Commit to backups
            log.info("2PC_COMMIT replicating tx_id=%s to %d backups", tx_id, len(self.backups))
            for i, backup in enumerate(self.backups):
                try:
                    backup.Commit(request, timeout=3)
                    log.info("2PC_COMMIT Backup %d confirmed commit tx_id=%s", i, tx_id)
                except Exception as e:
                    log.warning("2PC_COMMIT backup %d failed tx_id=%s err=%s", i, tx_id, e)
                    pass

        return com_resp

    def Abort(self, request, context):
        """2PC Abort: discard on primary, replicate to backups"""
        tx_id = (request.transaction_id or "").strip()
        
        # Replicate Abort to backups first
        log.info("2PC_ABORT replicating tx_id=%s to %d backups", tx_id, len(self.backups))
        for i, backup in enumerate(self.backups):
            try:
                backup.Abort(request, timeout=3)
                log.info("2PC_ABORT Backup %d confirmed abort tx_id=%s", i, tx_id)
            except Exception as e:
                log.warning("2PC_ABORT backup %d failed tx_id=%s err=%s", i, tx_id, e)
                pass
        
        # Then abort on primary
        return super().Abort(request, context)

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