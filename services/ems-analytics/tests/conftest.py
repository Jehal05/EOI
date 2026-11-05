import json, os, sys, warnings
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
warnings.filterwarnings("ignore")

ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "..")


def busbranch_from_cim_json(model_id):
    """Build the bus-branch view WITHOUT the Rust service by asking it... offline fixture:
    uses the cim-pds binary's topology via a saved fixture written by tools/export_fixtures.py."""
    p = os.path.join(ROOT, "data", f"{model_id}.busbranch.json")
    if not os.path.exists(p):
        pytest.skip(f"missing {p}; run tools/export_fixtures.sh")
    return json.load(open(p))


@pytest.fixture(scope="session")
def bb14():
    return busbranch_from_cim_json("ieee14")


@pytest.fixture(scope="session")
def bb118():
    return busbranch_from_cim_json("ieee118")
