"""只读检查地图的路线、可达性与单向出口。"""

from __future__ import annotations

from .models import WorldConfig
from .pathfinding import Graph, find_path, path_ticks


def _reachable(graph: Graph, start: str) -> set[str]:
    seen = {start}
    pending = [start]
    while pending:
        for neighbor, _ in graph.get(pending.pop(), []):
            if neighbor not in seen:
                seen.add(neighbor)
                pending.append(neighbor)
    return seen


def inspect_routes(world: WorldConfig, start: str, target: str = "") -> dict:
    nodes = world.node_map()
    if start not in nodes:
        raise ValueError("起点不在当前地图中")
    if target and target not in nodes:
        raise ValueError("终点不在当前地图中")
    graph = world.adjacent()
    reached = _reachable(graph, start)
    reverse: Graph = {node_id: [] for node_id in nodes}
    for source, edges in graph.items():
        for destination, ticks in edges:
            reverse.setdefault(destination, []).append((source, ticks))
    returns = _reachable(reverse, start)
    path = find_path(graph, start, target) if target else None
    return {
        "start": start, "target": target,
        "path": path,
        "ticks": path_ticks(graph, path) if path is not None else None,
        "reachable": [node_id for node_id in nodes if node_id in reached],
        "unreachable": [node_id for node_id in nodes if node_id not in reached],
        "no_return": [node_id for node_id in nodes if node_id in reached and node_id not in returns],
    }
