from collections.abc import Mapping
from typing import Protocol


class FPLDataProvider(Protocol):
    def get_bootstrap_static(self) -> Mapping[str, object]: ...

    def get_fixtures(self) -> list[Mapping[str, object]]: ...

    def get_player_summary(self, player_id: int) -> Mapping[str, object]: ...
