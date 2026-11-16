"""Steady background traffic against the gateway (about RPS requests per second)."""
import os
import random
import threading
import time

import httpx

URL = os.environ.get("TARGET", "http://gateway:8000/api/work")
RPS = float(os.environ.get("RPS", "20"))


def worker():
    with httpx.Client(timeout=10) as c:
        while True:
            try:
                c.post(URL)
            except httpx.HTTPError:
                pass
            time.sleep(random.expovariate(RPS / 8))


if __name__ == "__main__":
    for _ in range(8):
        threading.Thread(target=worker, daemon=True).start()
    while True:
        time.sleep(60)
