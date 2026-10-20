"""Process-local match coordination state (locks, timers and signal phase)."""
import asyncio

_RUNTIME: dict[str, dict] = {}


def get(match_id: str) -> dict:
    """Return one runtime object per match with all coordination primitives."""
    state = _RUNTIME.setdefault(match_id, {
        "lock": asyncio.Lock(),
        "lobby_lock": asyncio.Lock(),
        "group_refresh_lock": asyncio.Lock(),
        "tasks": [],
        "light": "red",
        "done": False,
        "round_finalizing": False,
        "phase_seq": 0,
        "phase_token": "0",
        "phase_history": {"0": {"light": "red"}},
        "player_phase_tokens": {},
        "player_grace_tokens": {},
        "player_phase_sync_failed": {},
        "player_rendered_version": {},
        "dm_locks": {},
        "state_version": 0,
        "grid": {},
        "grid_version": 0,
        "match_snapshot": None,
        "dirty_dms": set(),
        "move_queue": None,
        "group_dirty": False,
        "last_group_refresh": 0.0,
    })
    # Compatibility if an old runtime dict was created before a deployment.
    state.setdefault("group_refresh_lock", asyncio.Lock())
    state.setdefault("lobby_lock", asyncio.Lock())
    state.setdefault("dm_locks", {})
    state.setdefault("player_phase_tokens", {})
    state.setdefault("player_grace_tokens", {})
    state.setdefault("player_phase_sync_failed", {})
    state.setdefault("player_rendered_version", {})
    state.setdefault("phase_history", {})
    state.setdefault("phase_seq", 0)
    state.setdefault("phase_token", "0")
    state.setdefault("state_version", 0)
    state.setdefault("done", False)
    state.setdefault("round_finalizing", False)
    state.setdefault("grid", {})
    state.setdefault("grid_version", 0)
    state.setdefault("match_snapshot", None)
    state.setdefault("dirty_dms", set())
    state.setdefault("move_queue", None)
    state.setdefault("group_dirty", False)
    state.setdefault("last_group_refresh", 0.0)
    return state


def add_task(match_id: str, task: asyncio.Task) -> None:
    get(match_id)["tasks"].append(task)


def cancel_tasks(match_id: str) -> None:
    try:
        current = asyncio.current_task()
    except RuntimeError:
        current = None
    state = _RUNTIME.get(match_id)
    if not state:
        return
    for task in state.get("tasks", []):
        if task is not current and not task.done():
            task.cancel()
    state["tasks"] = []


def clear(match_id: str) -> None:
    cancel_tasks(match_id)
    _RUNTIME.pop(match_id, None)
