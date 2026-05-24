"""
Locust load testing script for Bookstore E2E Testing
Tests scenarios: single orders, multiple non-conflicting orders, mixed fraud/valid, conflicting orders
"""

from locust import HttpUser, task, between, events
import json
import random
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("locust_bookstore")

# Valid test data
VALID_USERS = [
    ("Alice Smith", "alice@example.com", "5425233010103442"),
    ("Bob Johnson", "bob@example.com", "3782822463100005"),
    ("David Lee", "david@example.com", "6011111111111117"),
    ("Eve Wilson", "eve@example.com", "3530111333300000"),
    ("Frank Miller", "frank@example.com", "4916627407375983"),
    ("Grace Lee", "grace@example.com", "4532015112830366"),
    ("Henry Wong", "henry@example.com", "5425233010103442"),
    ("Iris Chen", "iris@example.com", "3530111333300000"),
]

FRAUDULENT_USERS = [
    ("Fraud Attacker", "fraud@example.com", "4916627407375983", "fraud"),  # Contains 'fraud' in name
    ("Scam User", "scam@example.com", "5425233010103442", "scam"),  # Contains 'scam'
    ("Test User", "test@example.com", "3782822463100005", "test"),  # Contains 'test user'
    ("Charlie Brown", "charlie@example.com", "1111111111111111", "card"),  # Repeated card digits
]

BOOKS = [
    "The Great Gatsby",
    "1984",
]

BILLING_ADDRESSES = [
    {"street": "123 Main St", "city": "Springfield", "state": "IL", "zip": "62701", "country": "USA"},
    {"street": "456 Oak Ave", "city": "Portland", "state": "OR", "zip": "97201", "country": "USA"},
    {"street": "789 Elm St", "city": "Seattle", "state": "WA", "zip": "98101", "country": "USA"},
    {"street": "321 Pine Rd", "city": "Denver", "state": "CO", "zip": "80202", "country": "USA"},
]


def build_checkout_request(user_name, contact, card_number, books):
    """Build a valid checkout request"""
    return {
        "user": {
            "name": user_name,
            "contact": contact,
        },
        "creditCard": {
            "number": card_number,
            "expirationDate": f"{random.randint(1, 12):02d}/{random.randint(24, 27)}",
            "cvv": f"{random.randint(100, 999)}",
        },
        "items": books,
        "billingAddress": random.choice(BILLING_ADDRESSES),
        "shippingMethod": random.choice(["Standard", "Express", "Next-Day"]),
        "giftWrapping": random.choice([True, False]),
        "termsAccepted": True,
    }


class BookstoreUser(HttpUser):
    """Load test user for bookstore checkout"""

    # Wait 1-5 seconds between tasks
    wait_time = between(1, 5)

    def on_start(self):
        """Called when a simulated user starts executing this task set"""
        self.user_index = 0
        log.info("Bookstore user started")

    @task(3)
    def single_non_fraudulent_order(self):
        """Scenario 1: Single non-fraudulent order"""
        user_name, contact, card_number = random.choice(VALID_USERS)
        books = [{"name": random.choice(BOOKS), "quantity": random.randint(1, 3)}]
        
        payload = build_checkout_request(user_name, contact, card_number, books)
        
        with self.client.post(
            "/checkout",
            json=payload,
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                try:
                    data = response.json()
                    status = data.get("status", "Unknown")
                    order_id = data.get("orderId", "N/A")
                    log.info(f"[S1] Single order {order_id}: {status}")
                    response.success()
                except json.JSONDecodeError:
                    response.failure(f"Invalid JSON response: {response.text}")
            else:
                response.failure(f"Status {response.status_code}: {response.text}")

    @task(2)
    def multiple_non_conflicting_orders(self):
        """Scenario 2: Multiple non-conflicting orders (different books)"""
        # Pick 2 different users
        user1_name, user1_contact, user1_card = random.choice(VALID_USERS)
        user2_name, user2_contact, user2_card = random.choice(VALID_USERS)
        
        # Pick different books for each order
        book1 = random.choice(BOOKS)
        book2 = random.choice([b for b in BOOKS if b != book1])
        
        books1 = [{"name": book1, "quantity": random.randint(1, 2)}]
        books2 = [{"name": book2, "quantity": random.randint(1, 2)}]
        
        payload1 = build_checkout_request(user1_name, user1_contact, user1_card, books1)
        payload2 = build_checkout_request(user2_name, user2_contact, user2_card, books2)
        
        # Send both requests
        with self.client.post(
            "/checkout",
            json=payload1,
            catch_response=True,
        ) as response1:
            if response1.status_code == 200:
                try:
                    data1 = response1.json()
                    log.info(f"[S2.1] Order {data1.get('orderId', 'N/A')}: {data1.get('status', 'Unknown')}")
                    response1.success()
                except json.JSONDecodeError:
                    response1.failure(f"Invalid JSON in order 1: {response1.text}")
            else:
                response1.failure(f"Status {response1.status_code} in order 1")
        
        with self.client.post(
            "/checkout",
            json=payload2,
            catch_response=True,
        ) as response2:
            if response2.status_code == 200:
                try:
                    data2 = response2.json()
                    log.info(f"[S2.2] Order {data2.get('orderId', 'N/A')}: {data2.get('status', 'Unknown')}")
                    response2.success()
                except json.JSONDecodeError:
                    response2.failure(f"Invalid JSON in order 2: {response2.text}")
            else:
                response2.failure(f"Status {response2.status_code} in order 2")

    @task(2)
    def mixed_fraudulent_and_valid_orders(self):
        """Scenario 3: Mix of fraudulent and valid orders"""
        # 70% valid, 30% fraudulent
        if random.random() < 0.7:
            user_name, contact, card_number = random.choice(VALID_USERS)
        else:
            user_name, contact, card_number, _ = random.choice(FRAUDULENT_USERS)
        
        books = [{"name": random.choice(BOOKS), "quantity": random.randint(1, 3)}]
        payload = build_checkout_request(user_name, contact, card_number, books)
        
        with self.client.post(
            "/checkout",
            json=payload,
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                try:
                    data = response.json()
                    status = data.get("status", "Unknown")
                    order_id = data.get("orderId", "N/A")
                    fraud_label = " (FRAUDULENT)" if "Fraud" in user_name or "Scam" in user_name or "Test" in user_name or "Charlie" in user_name else ""
                    log.info(f"[S3] Mixed order {order_id}: {status}{fraud_label}")
                    response.success()
                except json.JSONDecodeError:
                    response.failure(f"Invalid JSON response: {response.text}")
            else:
                response.failure(f"Status {response.status_code}: {response.text}")

    @task(2)
    def conflicting_orders_race_condition(self):
        """Scenario 4: Conflicting orders (same book, race condition)"""
        # Multiple users ordering the same book
        user_name, contact, card_number = random.choice(VALID_USERS)
        book = "The Great Gatsby"  # Both will try to buy the same book
        
        books = [{"name": book, "quantity": random.randint(1, 2)}]
        payload = build_checkout_request(user_name, contact, card_number, books)
        
        with self.client.post(
            "/checkout",
            json=payload,
            catch_response=True,
        ) as response:
            if response.status_code == 200:
                try:
                    data = response.json()
                    status = data.get("status", "Unknown")
                    order_id = data.get("orderId", "N/A")
                    log.info(f"[S4] Conflicting order {order_id}: {status}")
                    response.success()
                except json.JSONDecodeError:
                    response.failure(f"Invalid JSON response: {response.text}")
            else:
                response.failure(f"Status {response.status_code}: {response.text}")


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    log.info("=" * 60)
    log.info("Bookstore Load Test Starting")
    log.info("=" * 60)
    log.info("Scenarios:")
    log.info("  [S1] Single non-fraudulent order (3x weight)")
    log.info("  [S2] Multiple non-conflicting orders (2x weight)")
    log.info("  [S3] Mixed fraudulent + valid orders (2x weight)")
    log.info("  [S4] Conflicting orders - race condition (2x weight)")
    log.info("=" * 60)


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    log.info("=" * 60)
    log.info("Bookstore Load Test Completed")
    log.info("=" * 60)
