# SPDX-License-Identifier: MIT-0
"""Rolling serving publication persistence over the real DynamoDB adapter fake."""

from __future__ import annotations

from copy import deepcopy
from datetime import date

import pytest
from execution_graph_dispatch_support import UNIT
from graph_publish_support import PublishWorld, Tables, publication, publish_world, seed_current

from adapters.outbound.dynamodb.execution_graph import due_shard
from domain.operations.execution_graph_dispatch import NodeState, serving_frontier_shard

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]


def _world() -> PublishWorld:
    fake = Tables()
    target = publication(date(2026, 9, 1), "rolling", 1)
    seed_current(fake, target, None)
    return publish_world(units={"db.table": (target,)}, fake=fake)


def test_publish_during_an_active_head_settles_on_the_live_frontier_without_parking() -> None:
    world = _world()
    head_key = next(key for key in world.fake.items if key[0].startswith("SERVING_DOMAIN#"))
    world.fake.items[head_key]["state"] = {"S": "ACTIVE"}
    head_before = deepcopy(world.fake.items[head_key])
    domain = head_key[0].removeprefix("SERVING_DOMAIN#")
    shard = serving_frontier_shard(world.run)
    frontier_key = (f"SERVING_FRONTIER#{domain}#{shard:02d}", "COUNTER")
    frontier_before = deepcopy(world.fake.items[frontier_key])

    result = world.publish(receive_count=1)

    assert (result.acknowledge, result.outcome) == (True, "graph_publish_succeeded")
    assert world.publish_node().state is NodeState.SUCCEEDED
    # The ACTIVE head is neither read into nor mutated by publication; no wait row exists.
    assert world.fake.items[head_key] == head_before
    assert world.store.read_due(due_shard(UNIT), "PUBLISH_WAIT#") == ()
    marker = world.fake.items[(f"SERVING_PUBLICATION#{world.run}", "MARKER")]
    assert marker["state"] == {"S": "SETTLED_ELIGIBLE"}
    frontier = world.fake.items[frontier_key]
    for name in ("visible_count", "eligible_count"):
        assert int(frontier[name]["N"]) == int(frontier_before[name]["N"]) + 1
    assert int(marker["eligible_coordinate"]["N"]) == int(frontier["eligible_count"]["N"])
