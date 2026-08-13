"""The DAG engine: ordering, parallelism, retries, optionality, checkpointing."""

from __future__ import annotations

import asyncio

import pytest

from aic.domain.models import Incident
from aic.orchestration.checkpoint import InMemoryCheckpointStore
from aic.orchestration.graph import Graph, GraphDefinitionError, Node
from aic.orchestration.state import InvestigationState, NodeStatus


def new_state() -> InvestigationState:
    return InvestigationState(run_id="run", incident=Incident(title="t"))


def recorder(log: list[str], name: str, *, fail_times: int = 0) -> object:
    attempts = {"n": 0}

    async def run(_: InvestigationState) -> None:
        attempts["n"] += 1
        if attempts["n"] <= fail_times:
            raise RuntimeError(f"{name} transient failure {attempts['n']}")
        log.append(name)

    return run


class TestDefinition:
    def test_cycle_is_rejected_at_build_time(self) -> None:
        with pytest.raises(GraphDefinitionError, match="cycle"):
            Graph.build(
                [
                    Node(name="a", run=recorder([], "a"), depends_on=("b",)),  # type: ignore[arg-type]
                    Node(name="b", run=recorder([], "b"), depends_on=("a",)),  # type: ignore[arg-type]
                ]
            )

    def test_unknown_dependency_is_rejected(self) -> None:
        with pytest.raises(GraphDefinitionError, match="unknown node"):
            Graph.build([Node(name="a", run=recorder([], "a"), depends_on=("ghost",))])  # type: ignore[arg-type]

    def test_duplicate_node_name_is_rejected(self) -> None:
        with pytest.raises(GraphDefinitionError, match="duplicate"):
            Graph.build(
                [
                    Node(name="a", run=recorder([], "a")),  # type: ignore[arg-type]
                    Node(name="a", run=recorder([], "a2")),  # type: ignore[arg-type]
                ]
            )

    def test_levels_group_independent_nodes_together(self) -> None:
        graph = Graph.build(
            [
                Node(name="root", run=recorder([], "root")),  # type: ignore[arg-type]
                Node(name="left", run=recorder([], "left"), depends_on=("root",)),  # type: ignore[arg-type]
                Node(name="right", run=recorder([], "right"), depends_on=("root",)),  # type: ignore[arg-type]
                Node(name="join", run=recorder([], "join"), depends_on=("left", "right")),  # type: ignore[arg-type]
            ]
        )
        assert [tuple(n.name for n in level) for level in graph.levels] == [
            ("root",),
            ("left", "right"),
            ("join",),
        ]


class TestExecution:
    async def test_runs_in_dependency_order(self) -> None:
        log: list[str] = []
        graph = Graph.build(
            [
                Node(name="first", run=recorder(log, "first")),  # type: ignore[arg-type]
                Node(name="second", run=recorder(log, "second"), depends_on=("first",)),  # type: ignore[arg-type]
            ]
        )
        await graph.run(new_state())
        assert log == ["first", "second"]

    async def test_independent_nodes_run_concurrently(self) -> None:
        """Two 100ms sleeps at the same level must not take 200ms."""
        started: list[float] = []

        async def slow(_: InvestigationState) -> None:
            started.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.1)

        graph = Graph.build(
            [
                Node(name="a", run=slow),
                Node(name="b", run=slow),
            ]
        )
        loop_start = asyncio.get_running_loop().time()
        await graph.run(new_state())
        elapsed = asyncio.get_running_loop().time() - loop_start

        assert len(started) == 2
        assert elapsed < 0.18, f"nodes appear to have run serially ({elapsed:.3f}s)"

    async def test_retries_then_succeeds(self) -> None:
        log: list[str] = []
        graph = Graph.build(
            [
                Node(
                    name="flaky",
                    run=recorder(log, "flaky", fail_times=2),  # type: ignore[arg-type]
                    retries=2,
                    retry_backoff_seconds=0.0,
                )
            ]
        )
        state = await graph.run(new_state())

        assert log == ["flaky"]
        assert state.trace("flaky").status is NodeStatus.SUCCEEDED
        assert state.trace("flaky").attempts == 3

    async def test_required_failure_aborts_downstream_work(self) -> None:
        log: list[str] = []
        graph = Graph.build(
            [
                Node(
                    name="required",
                    run=recorder(log, "required", fail_times=99),  # type: ignore[arg-type]
                    retry_backoff_seconds=0.0,
                ),
                Node(name="downstream", run=recorder(log, "downstream"), depends_on=("required",)),  # type: ignore[arg-type]
            ]
        )
        state = await graph.run(new_state())

        assert log == []
        assert state.trace("required").status is NodeStatus.FAILED
        assert state.trace("downstream").status is NodeStatus.PENDING
        assert state.errors

    async def test_optional_failure_degrades_instead_of_aborting(self) -> None:
        """This is what keeps a flaky tool integration from killing an investigation."""
        log: list[str] = []
        graph = Graph.build(
            [
                Node(
                    name="optional",
                    run=recorder(log, "optional", fail_times=99),  # type: ignore[arg-type]
                    optional=True,
                    retry_backoff_seconds=0.0,
                ),
                Node(name="after", run=recorder(log, "after")),  # type: ignore[arg-type]
            ]
        )
        state = await graph.run(new_state())

        assert "after" in log
        assert state.trace("optional").status is NodeStatus.FAILED

    async def test_dependent_of_failed_optional_node_is_skipped_not_failed(self) -> None:
        log: list[str] = []
        graph = Graph.build(
            [
                Node(
                    name="optional",
                    run=recorder(log, "optional", fail_times=99),  # type: ignore[arg-type]
                    optional=True,
                    retry_backoff_seconds=0.0,
                ),
                Node(name="child", run=recorder(log, "child"), depends_on=("optional",)),  # type: ignore[arg-type]
            ]
        )
        state = await graph.run(new_state())

        assert state.trace("child").status is NodeStatus.SKIPPED
        assert "child" not in log

    async def test_node_timeout_is_enforced(self) -> None:
        async def hang(_: InvestigationState) -> None:
            await asyncio.sleep(5)

        graph = Graph.build([Node(name="hang", run=hang, timeout_seconds=0.05)])
        state = await graph.run(new_state())

        assert state.trace("hang").status is NodeStatus.FAILED

    async def test_checkpoint_written_after_every_level(self) -> None:
        store = InMemoryCheckpointStore()
        graph = Graph.build(
            [
                Node(name="a", run=recorder([], "a")),  # type: ignore[arg-type]
                Node(name="b", run=recorder([], "b"), depends_on=("a",)),  # type: ignore[arg-type]
            ]
        )
        state = await graph.run(new_state(), checkpoints=store)

        restored = await store.load(state.run_id)
        assert restored is not None
        assert restored.trace("b").status is NodeStatus.SUCCEEDED

    async def test_finished_at_is_always_set(self) -> None:
        graph = Graph.build([Node(name="a", run=recorder([], "a"))])  # type: ignore[arg-type]
        state = await graph.run(new_state())
        assert state.finished_at is not None
