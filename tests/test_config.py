from pathlib import Path

from freight_sim.config import config_hash, load_config

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_default_config_loads_and_validates():
    config, digest = load_config(REPO_ROOT / "config" / "default.yaml")
    assert config.run.seed == 42
    assert config.fleet.num_trucks > 0
    assert len(config.demand.zones) >= 2
    assert isinstance(digest, str) and len(digest) == 16


def test_config_hash_is_deterministic_and_content_sensitive():
    assert config_hash("a: 1") == config_hash("a: 1")
    assert config_hash("a: 1") != config_hash("a: 2")
