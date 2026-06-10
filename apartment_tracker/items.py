"""Registry of tracked items and the tags that identify them.

An item is anything the user wants to find. Tags map sensor-level identities
(ArUco ID, BLE MAC, detector class label) onto an item, so one physical item
can be recognized by several modalities at once and modalities can be added
later without re-registering items.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Item:
    item_id: str
    name: str
    labels: list[str] = field(default_factory=list)  # detector class names
    tag_ids: list[str] = field(default_factory=list)  # "aruco:7", "ble:AA:BB:..", "rfid:.."


class ItemRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Item] = {}
        self._by_tag: dict[str, str] = {}
        self._by_label: dict[str, str] = {}

    def add(self, item: Item) -> None:
        if item.item_id in self._items:
            raise ValueError(f"duplicate item_id {item.item_id!r}")
        self._items[item.item_id] = item
        for tag in item.tag_ids:
            self._by_tag[tag] = item.item_id
        for label in item.labels:
            self._by_label[label] = item.item_id

    def tag_item(self, item_id: str, tag_id: str) -> None:
        """Manually attach a new tag to an existing item at runtime."""
        item = self._items[item_id]
        item.tag_ids.append(tag_id)
        self._by_tag[tag_id] = item_id

    def get(self, item_id: str) -> Item | None:
        return self._items.get(item_id)

    def resolve_tag(self, tag_id: str) -> str | None:
        return self._by_tag.get(tag_id)

    def resolve_label(self, label: str) -> str | None:
        return self._by_label.get(label)

    def all(self) -> list[Item]:
        return list(self._items.values())

    @classmethod
    def from_config(cls, item_cfgs: list[dict]) -> "ItemRegistry":
        reg = cls()
        for c in item_cfgs:
            reg.add(
                Item(
                    item_id=c["id"],
                    name=c.get("name", c["id"]),
                    labels=list(c.get("labels", [])),
                    tag_ids=[str(t) for t in c.get("tags", [])],
                )
            )
        return reg
