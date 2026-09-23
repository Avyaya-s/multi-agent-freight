"""One-off: download and cache the road network so simulation runs never hit
OSM/Overpass live. Run this once; RoadNetwork.load() reuses the cache after."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from freight_sim.config import load_config
from freight_sim.network import RoadNetwork

if __name__ == "__main__":
    config, _ = load_config(Path(__file__).resolve().parent.parent / "config" / "default.yaml")
    print(f"Fetching road network for bbox {config.network.bbox} ...")
    network = RoadNetwork.load(config.network)
    print(f"Graph cached at {config.network.cache_path}")
    print(f"Nodes: {network.graph.number_of_nodes()}, edges: {network.graph.number_of_edges()}")
