import pytest

from app.domain.policy import load_policy
from tests.paths import POLICY_PATH


@pytest.fixture(scope="session")
def policy():
    return load_policy(POLICY_PATH)
