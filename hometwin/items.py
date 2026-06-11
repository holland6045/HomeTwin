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
        self._by_label: dict[str, list[str]] = {}

    def add(self, item: Item) -> None:
        if item.item_id in self._items:
            raise ValueError(f"duplicate item_id {item.item_id!r}")
        self._items[item.item_id] = item
        for tag in item.tag_ids:
            self._by_tag[tag] = item.item_id
        for label in item.labels:
            self._by_label.setdefault(label, []).append(item.item_id)

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
        """Unambiguous label -> item; None when zero or several items share it
        (identical storage boxes — the fusion engine then associates by
        proximity to existing tracks instead)."""
        candidates = self._by_label.get(label, [])
        return candidates[0] if len(candidates) == 1 else None

    def label_candidates(self, label: str) -> list[str]:
        return list(self._by_label.get(label, []))

    def all(self) -> list[Item]:
        return list(self._items.values())

    @classmethod
    def from_config(
        cls, item_cfgs: list[dict], set_cfgs: list[dict] | None = None
    ) -> "ItemRegistry":
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
        # item_sets: families of visually identical items (storage boxes)
        # distinguished only by sequential tags
        for s in set_cfgs or []:
            count = int(s["count"])
            first = int(s.get("first_tag_id", 0))
            kind = s.get("tag_kind", "aruco")
            width = max(2, len(str(count)))
            prefix = s["id_prefix"]
            name_prefix = s.get("name_prefix", prefix.replace("-", " ").strip().title() + " ")
            for i in range(1, count + 1):
                reg.add(
                    Item(
                        item_id=f"{prefix}{i:0{width}d}",
                        name=f"{name_prefix}{i:0{width}d}",
                        labels=list(s.get("labels", [])),
                        tag_ids=[f"{kind}:{first + i - 1}"],
                    )
                )
        return reg
