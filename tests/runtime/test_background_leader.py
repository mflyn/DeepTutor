from __future__ import annotations

import asyncio

import pytest

from deeptutor.runtime.background_leader import BackgroundLeaderSupervisor
from deeptutor.runtime.coordination import MemoryCoordinator


@pytest.mark.asyncio
async def test_only_one_supervisor_runs_services_and_successor_takes_over() -> None:
    coordinator = MemoryCoordinator(lease_ttl_seconds=0.08)
    running: set[str] = set()
    max_running = 0

    def callbacks(worker_id: str):
        async def start() -> None:
            nonlocal max_running
            running.add(worker_id)
            max_running = max(max_running, len(running))

        async def stop() -> None:
            running.discard(worker_id)

        return start, stop

    start_a, stop_a = callbacks("a")
    start_b, stop_b = callbacks("b")
    first = BackgroundLeaderSupervisor(
        coordinator,
        "a",
        start_callbacks=[start_a],
        stop_callbacks=[stop_a],
        renew_interval_seconds=0.02,
        election_interval_seconds=0.01,
    )
    second = BackgroundLeaderSupervisor(
        coordinator,
        "b",
        start_callbacks=[start_b],
        stop_callbacks=[stop_b],
        renew_interval_seconds=0.02,
        election_interval_seconds=0.01,
    )
    await first.start()
    await second.start()
    await asyncio.sleep(0.05)
    assert len(running) == 1
    assert max_running == 1

    leader = next(iter(running))
    await (first if leader == "a" else second).close()
    await asyncio.sleep(0.05)
    assert running == ({"b"} if leader == "a" else {"a"})
    assert max_running == 1

    await first.close()
    await second.close()


@pytest.mark.asyncio
async def test_background_commands_run_once_and_continue_after_leader_transfer() -> None:
    coordinator = MemoryCoordinator(lease_ttl_seconds=0.08)
    handled: list[tuple[str, str]] = []

    def callback(worker_id: str):
        async def handle(command) -> None:
            handled.append((worker_id, command.command_id))

        return handle

    first = BackgroundLeaderSupervisor(
        coordinator,
        "a",
        control_callback=callback("a"),
        renew_interval_seconds=0.02,
        election_interval_seconds=0.01,
    )
    second = BackgroundLeaderSupervisor(
        coordinator,
        "b",
        control_callback=callback("b"),
        renew_interval_seconds=0.02,
        election_interval_seconds=0.01,
    )
    await coordinator.submit_background_command("cron_reload", command_id="before")
    await first.start()
    await second.start()
    await asyncio.sleep(0.05)
    assert [command_id for _, command_id in handled] == ["before"]

    leader = await coordinator.leader_id()
    assert leader in {"a", "b"}
    await (first if leader == "a" else second).close()
    await asyncio.sleep(0.05)
    await coordinator.submit_background_command("cron_reload", command_id="after")
    await asyncio.sleep(0.05)

    assert [command_id for _, command_id in handled] == ["before", "after"]
    assert handled[-1][0] != leader
    await first.close()
    await second.close()


@pytest.mark.asyncio
async def test_slow_service_start_keeps_leader_lease_alive() -> None:
    coordinator = MemoryCoordinator(lease_ttl_seconds=0.05)
    started = asyncio.Event()
    release_start = asyncio.Event()
    contenders: list[str] = []

    async def slow_start() -> None:
        started.set()
        await release_start.wait()

    supervisor = BackgroundLeaderSupervisor(
        coordinator,
        "leader",
        start_callbacks=[slow_start],
        stop_callbacks=[lambda: asyncio.sleep(0)],
        renew_interval_seconds=0.01,
        election_interval_seconds=0.005,
    )
    await supervisor.start()
    await asyncio.wait_for(started.wait(), timeout=0.1)
    await asyncio.sleep(0.08)
    contender = await coordinator.acquire_leader("contender")
    if contender is not None:
        contenders.append(contender.owner_id)

    assert contenders == []
    assert await coordinator.leader_id() == "leader"

    release_start.set()
    await supervisor.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_target", ["lost", "operation"])
async def test_close_preserves_parent_cancellation_during_child_cleanup(
    cleanup_target: str,
) -> None:
    coordinator = MemoryCoordinator()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    recovery_started = asyncio.Event()
    release_recovery = asyncio.Event()
    callbacks: list[str] = []

    class CleanupBarrier(asyncio.Event):
        async def wait(self) -> bool:
            try:
                return await super().wait()
            finally:
                cleanup_entered.set()
                await release_cleanup.wait()

    async def start() -> None:
        callbacks.append("start")

    async def stop() -> None:
        callbacks.append("stop")

    async def recover() -> None:
        recovery_started.set()
        try:
            await release_recovery.wait()
        finally:
            cleanup_entered.set()
            await release_cleanup.wait()

    supervisor = BackgroundLeaderSupervisor(
        coordinator,
        "cleanup-race",
        start_callbacks=[start],
        stop_callbacks=[stop],
        recovery_callback=recover if cleanup_target == "operation" else None,
    )
    if cleanup_target == "lost":
        supervisor._leadership_lost = CleanupBarrier()
    close_task = None
    heartbeat = None
    await supervisor.start()
    leader = supervisor._task
    try:
        if cleanup_target == "operation":
            await asyncio.wait_for(recovery_started.wait(), timeout=1)
            supervisor._leadership_lost.set()
        await asyncio.wait_for(cleanup_entered.wait(), timeout=1)
        heartbeat = supervisor._heartbeat_task
        assert leader is not None and not leader.done()
        assert heartbeat is not None and not heartbeat.done()
        assert callbacks == ["start"]
        assert await coordinator.leader_id() == "cleanup-race"

        close_task = asyncio.create_task(supervisor.close())
        # Observation must not inject a second cancellation that rescues a hung close.
        done, _ = await asyncio.wait({close_task}, timeout=1)
        assert close_task in done, "close swallowed parent cancellation during child cleanup"
        await close_task
        assert leader.cancelled()
        assert heartbeat.done()
        assert callbacks == ["start", "stop"]
        assert supervisor._task is None
        assert supervisor._heartbeat_task is None
        assert not supervisor._services_running
        assert supervisor._started_service_count == 0
        assert supervisor.lease is None
        assert await coordinator.leader_id() is None
    finally:
        # Only after the observation/assertions: release and reap tasks on the red path.
        release_cleanup.set()
        release_recovery.set()
        if leader is not None and not leader.done():
            leader.cancel()
        if close_task is None:
            close_task = asyncio.create_task(supervisor.close())
        tasks = {task for task in (leader, close_task) if task is not None}
        _, pending = await asyncio.wait(tasks, timeout=1)
        for task in pending:
            task.cancel()
        if pending:
            _, pending = await asyncio.wait(pending, timeout=1)
        assert not pending, "test cleanup left leader tasks running"
        await asyncio.gather(*tasks, return_exceptions=True)
        await supervisor.close()
        await coordinator.close()
