"""Threading + acceleration: parallel polling, provider ordering, RTI cache."""

import time

from hometwin.accel import capabilities, onnx_providers, poll_workers
from hometwin.config import AppConfig
from hometwin.items import ItemRegistry
from hometwin.sensors.base import SensorAdapter
from hometwin.sensors.tomography import RTIGrid
from hometwin.tracker import Tracker
from hometwin.world import World


class SleepySensor(SensorAdapter):
    """Stands in for an MJPEG camera blocking on the network."""

    def __init__(self, sensor_id, delay=0.05):
        super().__init__(sensor_id)
        self.delay = delay

    def poll(self):
        time.sleep(self.delay)
        return []


def run_step(parallel):
    sensors = [SleepySensor(f"s{i}") for i in range(4)]
    cfg = AppConfig(world=World(), items=ItemRegistry(), sensors=sensors,
                    parallel_polling=parallel)
    tracker = Tracker(cfg)
    t0 = time.perf_counter()
    tracker.step()
    return time.perf_counter() - t0


def test_parallel_polling_overlaps_sensor_io():
    serial = run_step(parallel=False)
    parallel = run_step(parallel=True)
    assert serial >= 0.18     # 4 x 50 ms strictly serialized
    assert parallel < 0.13    # overlapped: ~one delay + pool overhead


def test_poll_failure_isolated_in_parallel_mode():
    class Bad(SensorAdapter):
        def poll(self):
            raise RuntimeError("boom")

    class Good(SensorAdapter):
        def __init__(self):
            super().__init__("good")
            self.polled = 0

        def poll(self):
            self.polled += 1
            return []

    good = Good()
    cfg = AppConfig(world=World(), items=ItemRegistry(),
                    sensors=[Bad("bad"), good], parallel_polling=True)
    Tracker(cfg).step()
    assert good.polled == 1  # one sensor exploding never starves the rest


def test_onnx_provider_ordering():
    assert onnx_providers([]) == ["CPUExecutionProvider"]
    ranked = onnx_providers(
        ["CPUExecutionProvider", "CUDAExecutionProvider", "TensorrtExecutionProvider"])
    assert ranked[0] == "TensorrtExecutionProvider"
    assert ranked[1] == "CUDAExecutionProvider"
    assert ranked[-1] == "CPUExecutionProvider"


def test_capabilities_report():
    caps = capabilities()
    assert caps["cpu_count"] >= 1
    assert "onnx_providers" in caps and "cv2_cuda_devices" in caps
    assert 1 <= poll_workers(100) <= 16


def test_rti_mask_cache_matches_bruteforce():
    import math

    nodes = {"a": (0.0, 0.0), "b": (4.0, 0.0), "c": (4.0, 4.0)}
    grid = RTIGrid(nodes, (0, 0, 4, 4), cell_m=0.5)
    atten = {("a", "c"): 5.0, ("a", "b"): 3.0}
    img = grid.reconstruct(atten)

    # brute-force reference: the original O(links*cells) computation
    ref = [[0.0] * grid.nx for _ in range(grid.ny)]
    for (a, b), w0 in atten.items():
        ax, ay = nodes[a]
        bx, by = nodes[b]
        link = math.hypot(bx - ax, by - ay)
        w = w0 / math.sqrt(link)
        for iy in range(grid.ny):
            for ix in range(grid.nx):
                px, py = grid.cell_center(ix, iy)
                if math.hypot(px - ax, py - ay) + math.hypot(px - bx, py - by) - link < grid.lam:
                    ref[iy][ix] += w
    for row_img, row_ref in zip(img, ref):
        for v_img, v_ref in zip(row_img, row_ref):
            assert abs(v_img - v_ref) < 1e-9
    assert len(grid._link_cells) == 2  # masks cached for reuse
