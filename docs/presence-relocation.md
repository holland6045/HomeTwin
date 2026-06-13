# People tracking & relocation inference

Person detections (RT-DETR `person`, or any class listed in
`tracker.presence_labels`) are routed into the **fusion engine** — the
same Kalman/Mahalanobis estimator that tracks items — but kept in a
separate `people` registry. They are never items: they don't appear in
`/items`, aren't persisted across restarts, and can't seed or hijack an
item track.

## What people tracking gives you

- **Smoothed position + velocity** per occupant, not a per-frame dot.
- **Multi-person association**: each detection associates to the nearest
  gated existing occupant (predicted forward to the frame time) or spawns
  a new `person-N`; two people in frame stay two tracks.
- **Departure**: an occupant unseen for `presence_stale_after_s` (default
  12 s) is dropped.
- Surfaces at `GET /people`, the dashboard's **People** layer (markers,
  velocity arrows, trails on map and projected into the camera view), and
  as motion-zone evidence using the *filtered* position (less jitter than
  raw detections).

## Relocation inference (the mobile drawer)

The drawer-stow guess — "keys last seen at the open drawer, drawer now
closed → keys likely inside" — generalizes to a person as a mobile
container:

1. **Contact**: while an item is still being seen, the system notes any
   occupant within `REACH_M` (0.8 m, horizontal) of it.
2. **Pickup**: if the item then goes quiet (tag pocketed, object occluded
   in a hand) within ~`PICKUP_QUIET_S` of that contact, it's presumed
   carried by that person.
3. **Follow**: the item's likely location tracks the carrier. When the
   carrier's speed drops below `CARRY_SETTLE_SPEED` they're setting it
   down; that position freezes as the likely spot. If the carrier leaves
   before settling, the likely spot freezes at where they were last seen.
4. **Release**: if the item is seen again, the live track wins and the
   hypothesis is dropped; abandoned carries expire after `CARRY_MAX_AGE_S`.

Output appears on the item as `maybe_carried_by` + `likely_zone` /
`likely_position`, in the dashboard item list, and in the CLI:

```
$ hometwin where keys
House keys: likely carried by person-2 to sofa near (5.0, 5.0, 0.0) (last seen itself in counter)
```

Like the drawer inference, this **annotates** rather than moving the
Kalman track — the item's own estimate stays at its last real fix; the
carry is a separate, clearly-labelled hypothesis. Tuning constants live
at the top of `hometwin/tracker.py`.
