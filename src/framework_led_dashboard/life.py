from __future__ import annotations

from collections.abc import Callable


LIFE_WIDTH = 9
LIFE_HEIGHT = 12
LifeGrid = tuple[tuple[bool, ...], ...]
SeedFactory = Callable[[], LifeGrid]


def evolve(state: LifeGrid) -> LifeGrid:
    _validate_grid(state)
    next_rows: list[tuple[bool, ...]] = []
    for y in range(LIFE_HEIGHT):
        next_row: list[bool] = []
        for x in range(LIFE_WIDTH):
            neighbours = sum(
                state[(y + offset_y) % LIFE_HEIGHT][(x + offset_x) % LIFE_WIDTH]
                for offset_y in (-1, 0, 1)
                for offset_x in (-1, 0, 1)
                if offset_x != 0 or offset_y != 0
            )
            alive = state[y][x]
            next_row.append(neighbours == 3 or alive and neighbours == 2)
        next_rows.append(tuple(next_row))
    return tuple(next_rows)


class LifeAnimation:
    """Conway animation that replaces dead, still, or short repeating states."""

    def __init__(self, *, initial_state: LifeGrid, seed_factory: SeedFactory) -> None:
        _validate_grid(initial_state)
        self._state = initial_state
        self._seed_factory = seed_factory
        self._history = [initial_state]

    @property
    def state(self) -> LifeGrid:
        return self._state

    def step(self) -> LifeGrid:
        candidate = evolve(self._state)
        if not _has_live_cells(candidate) or candidate in self._history:
            candidate = self._seed_factory()
            _validate_grid(candidate)
            self._history = [candidate]
        else:
            self._history.append(candidate)
            self._history = self._history[-8:]

        self._state = candidate
        return candidate


def _has_live_cells(state: LifeGrid) -> bool:
    return any(any(row) for row in state)


def _validate_grid(state: LifeGrid) -> None:
    if len(state) != LIFE_HEIGHT or any(len(row) != LIFE_WIDTH for row in state):
        raise ValueError(f"Life grid must be {LIFE_WIDTH}x{LIFE_HEIGHT}")
