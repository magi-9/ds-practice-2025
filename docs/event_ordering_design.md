# Seminar 5 event ordering design

## 6-event partial order

Event a: transaction-verification service verifies if the order items (books) are not an empty list.
Event b: transaction-verification service verifies if all mandatory user data (name, contact, address…) is filled in.
Event c: transaction-verification service verifies if the credit card information is in the correct format.
Event d: fraud-detection service checks the user data for fraud.
Event e: fraud-detection service checks the credit card data for fraud.
Event f: suggestions service generates book suggestions.

**Relation between these events:**
(a) and (b) can be initiated in parallel (a ‖ b).
(c) can only occur after (a) completes, but it can overlap with (b) if (a) finishes first.
(d) can only occur after (b) completes, but it can overlap with (c) if (b) finishes first.
(e) depends on both (c) and (d) having completed.
(f) depends on (e).

**Visual diagram**
![Order diagram](/docs/images/eventorderdiagram.png)

**Vector clocks diagram**
![Vector clocks diagram](/docs/images/eventorderdiagram.png)

## Vector Clock Rules

**Vector clock structure**
Each service uses a dictionary:
{"transaction_verification": 0, "fraud_detection": 0, "suggestions": 0}

**Clock initialization per OrderID**
When the orchestrator calls InitializeOrder() on a service, that service then creates a new vector clock. New vector clock is set to a zero for the given OrderID and stores it in memory together with order data.
{"transaction_verification": 0, "fraud_detection": 0, "suggestions": 0}

**Local increment on event**
Every time a serivce executes an event, it needs to mark that it did something. It does this by +1 to its own slot in the vector clock.
state.vector_clock[self._service_name] += 1

So if transaction_verifiction runs event_a, only the transaction_verification slot goes up by 1. For example: 
Before: {"transaction_verification": 0, "fraud_detection": 0, "suggestions": 0}
After: {"transaction_verification": 1, "fraud_detection": 0, "suggestions": 0}

**Merge/update on received messages**
When a service receives a message from another service, it needs to catch up on what the other service has done. It does this by comparing its own vetor clock with the incoming vector clock and taking the maximum value for each slot. 

If the other service has a higher number in any slot, we update our own slot to match it. But this happens BEFORE increasing our own slot, because we first want to know what the other service knew and then record our own new action.

For example fraud_detection receives a message from transaction_verification with vector clock [2,0,0]:
fraud_detection local clock: {"transaction_verification": 1, "fraud_detection": 0, "suggestions": 0}
after merge: {"transaction_verification": 2, "fraud_detection": 0, "suggestions": 0}
after increasing the slot: {"transaction_verification": 2, "fraud_detection": 1, "suggestions": 0}

**Logging format**
After each event, the following is logged. Each log entry contains the 
order ID, the event name, the current vector clock, whether the event 
succeeded, and the reason.

For example:
order_id=123 event=a vc={"transaction_verification": 1, "fraud_detection": 0, "suggestions": 0} success=True reason="Items list is valid"

## Shared Data Contract

**OrderID**
The orchestrator generates a unique order_id for each order. 

**Vector clock**
Every request and response includes a vector clock:
{"transaction_verification": 0, "fraud_detection": 0, "suggestions": 0}

**Failure format**
If any event fails, the service immediately returns a response with success set to false and a reason string explainig what went wrong. The orchestrator then stops the entire flow and returns the error to the user.
For example: success=false, reason="Missing user name"

**Success response**
If all events complete successfully then the suggestions service returns 
a list of recommended books to the orchestrator which then sends 
them back to the user.
For example: {"book_id": "101", "title": "The Great Gatsby", "author": "F. Scott Fitzgerald"}