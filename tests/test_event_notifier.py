import asyncio

from backend.agent_runtime.event_notifier import RunEventNotifier


def test_notifier_wakes_all_subscribers_from_worker_thread_and_scopes_by_run():
    async def scenario():
        notifier = RunEventNotifier()
        first = notifier.subscribe("run-a")
        second = notifier.subscribe("run-a")
        other_run = notifier.subscribe("run-b")

        await asyncio.to_thread(notifier.notify, "run-a")
        await asyncio.wait_for(first.wait(), timeout=0.2)
        await asyncio.wait_for(second.wait(), timeout=0.2)
        assert not other_run.is_set()

        first.clear()
        notifier.unsubscribe("run-a", first)
        await asyncio.to_thread(notifier.notify, "run-a")
        await asyncio.wait_for(second.wait(), timeout=0.2)
        assert not first.is_set()

        notifier.unsubscribe("run-a", second)
        notifier.unsubscribe("run-b", other_run)

    asyncio.run(scenario())
