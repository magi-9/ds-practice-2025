import sys
import os
import json
import random
import logging

# This set of lines are needed to import the gRPC stubs.
# The path of the stubs is relative to the current file, or absolute inside the container.
# Change these lines only if strictly needed.
FILE = __file__ if '__file__' in globals() else os.getenv("PYTHONFILE", "")
pb_root = os.path.abspath(os.path.join(FILE, "../../../../utils/pb"))
sys.path.insert(0, pb_root)

from suggestions import suggestions_pb2 as sg_pb2
from suggestions import suggestions_pb2_grpc as sg_grpc

import grpc
from concurrent import futures

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("suggestions")


def summarize_order(order):
    items = order.get("items") or []
    user = order.get("user") or {}

    return {
        "item_count": len(items),
        "has_user_name": bool(user.get("name")),
        "has_user_contact": bool(user.get("contact")),
    }

# Static book catalog
BOOK_CATALOG = [
    {"book_id": "101", "title": "The Great Gatsby", "author": "F. Scott Fitzgerald"},
    {"book_id": "102", "title": "To Kill a Mockingbird", "author": "Harper Lee"},
    {"book_id": "103", "title": "1984", "author": "George Orwell"},
    {"book_id": "104", "title": "Pride and Prejudice", "author": "Jane Austen"},
    {"book_id": "105", "title": "The Catcher in the Rye", "author": "J.D. Salinger"},
    {"book_id": "106", "title": "Animal Farm", "author": "George Orwell"},
    {"book_id": "107", "title": "Lord of the Flies", "author": "William Golding"},
    {"book_id": "108", "title": "Brave New World", "author": "Aldous Huxley"},
    {"book_id": "109", "title": "The Hobbit", "author": "J.R.R. Tolkien"},
    {"book_id": "110", "title": "Fahrenheit 451", "author": "Ray Bradbury"},
]


class SuggestionsService(sg_grpc.SuggestionsServiceServicer):
    def GetSuggestions(self, request, context):
        log.info("GetSuggestions called")

        try:
            order = json.loads(request.order_json)
        except Exception as exc:
            # Return empty suggestions on invalid JSON
            log.warning("Invalid JSON payload received: %s", exc)
            return sg_pb2.SuggestionsResponse(books=[])

        log.info("Suggestion request summary: %s", summarize_order(order))

        # Simple logic: return 3 random books from catalog - later can be some user based history
        num_suggestions = min(3, len(BOOK_CATALOG))
        suggested_books = random.sample(BOOK_CATALOG, num_suggestions)

        # Protobuf convert
        books = [
            sg_pb2.Book(
                book_id=book["book_id"],
                title=book["title"],
                author=book["author"]
            )
            for book in suggested_books
        ]

        log.info(
            "Returning %s book suggestions with ids=%s",
            len(books),
            [book.book_id for book in books],
        )
        return sg_pb2.SuggestionsResponse(books=books)


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    sg_grpc.add_SuggestionsServiceServicer_to_server(SuggestionsService(), server)

    port = "50053"
    server.add_insecure_port("[::]:" + port)
    server.start()
    log.info("Suggestions service started on port %s with max_workers=%s", port, 10)
    server.wait_for_termination()


if __name__ == "__main__":
    serve()
