"""Measure customer search latency: ``python manage.py benchmark_customer_search``.

Creates a throwaway organization with N customers (default 50,000), runs typical searches
through the same selector as the API, prints p50/p95 per query type, then deletes the
organization again. Meant for PostgreSQL; M2.4's target is p95 < 100 ms at 50k customers.
"""

import random
import statistics
import time
import uuid

from django.core.management.base import BaseCommand
from django.db import connection

from bookings.models import Customer
from crm.selectors import search_filter
from organizations.models import Organization

FIRST = ["Ada", "Grace", "Alan", "Maya", "Leo", "Priya", "Tom", "Nora", "Sam", "Hana", "Omar"]
LAST = ["Lovelace", "Hopper", "Turing", "Chen", "Martin", "Shah", "Becker", "White", "Kim"]


class Command(BaseCommand):
    help = "Benchmark CRM customer search on a throwaway organization."

    def add_arguments(self, parser):
        parser.add_argument("--customers", type=int, default=50_000)
        parser.add_argument("--runs", type=int, default=40)

    def handle(self, *args, customers, runs, **options):
        rng = random.Random(42)
        organization = Organization.objects.create(
            name="Search benchmark", slug=f"bench-{uuid.uuid4().hex[:8]}"
        )
        try:
            self._populate(organization, customers, rng)
            queries = {
                "name word": lambda: rng.choice(LAST)[:5].lower(),
                "two words": lambda: f"{rng.choice(FIRST)} {rng.choice(LAST)[:4]}",
                "email part": lambda: f"user{rng.randrange(customers)}@",
                "phone (formatted)": lambda: f"(416) 555-{rng.randrange(10_000):04d}",
                "phone (partial)": lambda: f"{rng.randrange(100_000, 999_999)}",
                "no match": lambda: "zzqxj",
            }
            base = Customer.objects.filter(organization=organization)
            self.stdout.write(f"{customers:,} customers, {runs} runs per query type:")
            worst = 0.0
            for label, make_query in queries.items():
                timings = []
                for _ in range(runs):
                    query = make_query()
                    started = time.perf_counter()
                    list(search_filter(base, query).order_by("last_name", "first_name")[:20])
                    timings.append((time.perf_counter() - started) * 1000)
                timings.sort()
                p95 = timings[int(len(timings) * 0.95) - 1]
                worst = max(worst, p95)
                self.stdout.write(
                    f"  {label:<18} p50 {statistics.median(timings):6.1f} ms   p95 {p95:6.1f} ms"
                )
            style = self.style.SUCCESS if worst < 100 else self.style.ERROR
            self.stdout.write(style(f"Worst p95: {worst:.1f} ms (target < 100 ms)"))
        finally:
            organization.delete()

    def _populate(self, organization, count, rng):
        batch = []
        for index in range(count):
            phone = f"+1416555{index % 10_000:04d}" if index % 3 else f"+1647{index:07d}"[:12]
            customer = Customer(
                organization=organization,
                first_name=rng.choice(FIRST),
                last_name=f"{rng.choice(LAST)}{index % 97}",
                email=f"user{index}@example.test",
                phone=phone,
            )
            customer.sync_name()
            customer.phone_search = Customer.digits(phone)
            batch.append(customer)
            if len(batch) == 5000:
                Customer.objects.bulk_create(batch)
                batch = []
        Customer.objects.bulk_create(batch)
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("ANALYZE bookings_customer")
