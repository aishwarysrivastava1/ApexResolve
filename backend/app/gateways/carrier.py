"""CarrierGateway: tracking lookups. Prototype = MockCarrier over a JSON fixture file."""
import json

SUPPORTED_CARRIERS = ["BLUEDART", "DELHIVERY", "DTDC", "INDIA_POST"]


class MockCarrier:
    """Tracking lookups from a JSON fixture: {"BLUEDART:AWB10000001": {"status": "DELIVERED", "postcode": "110001"}}."""

    def __init__(self, fixtures):
        self.fixtures = fixtures

    @classmethod
    def from_file(cls, path):
        with open(path, "r", encoding="utf-8") as handle:
            return cls(json.load(handle))

    async def track(self, carrier, tracking_number):
        record = self.fixtures.get(f"{carrier}:{tracking_number}")
        if record is None:
            return {"status": "NOT_FOUND", "postcode": None}
        return {"status": record["status"], "postcode": record.get("postcode")}
