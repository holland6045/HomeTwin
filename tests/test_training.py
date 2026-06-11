import math

import pytest

from hometwin import registry
from hometwin.sensors.ble import PathLossModel
from hometwin.training.dataset import DatasetRecorder
from hometwin.training.trainers import PathLossTrainer


def test_path_loss_trainer_recovers_parameters():
    truth = PathLossModel(tx_power=-62.0, exponent=3.1)
    records = [
        {"rssi": truth.range_to_rssi(d), "dist_m": d}
        for d in (0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 8.0)
    ]
    fit = PathLossTrainer().train(records)
    assert fit["tx_power"] == pytest.approx(-62.0, abs=1e-6)
    assert fit["exponent"] == pytest.approx(3.1, abs=1e-6)


def test_path_loss_trainer_rejects_degenerate_data():
    with pytest.raises(ValueError):
        PathLossTrainer().train([{"rssi": -60, "dist_m": 1.0}])
    with pytest.raises(ValueError):
        PathLossTrainer().train([{"rssi": -60, "dist_m": 1.0}, {"rssi": -61, "dist_m": 1.0}])


def test_dataset_roundtrip(tmp_path):
    rec = DatasetRecorder(tmp_path)
    rec.record_rssi_sample("ble-1", "AA:BB", -64.0, 1.8)
    rec.record_detection("keys", (0.1, 0.2, 0.05, 0.05), "img/001.jpg", "cam-1")
    rssi = rec.load("rssi")
    assert len(rssi) == 1 and rssi[0]["dist_m"] == 1.8
    dets = rec.load("detections")
    assert dets[0]["label"] == "keys" and dets[0]["correct"] is True
    assert rec.load("nonexistent") == []


def test_retrain_loop_improves_ranging(tmp_path):
    """Capture samples under a mismatched model, retrain, verify error drops."""
    env = PathLossModel(tx_power=-65.0, exponent=3.3)  # real apartment
    assumed = PathLossModel()  # factory defaults
    rec = DatasetRecorder(tmp_path)
    for d in (0.5, 1.0, 2.0, 3.0, 4.0, 6.0):
        rec.record_rssi_sample("ble-1", "AA:BB", env.range_to_rssi(d), d)

    fit = PathLossTrainer().train(rec.load("rssi"))
    trained = PathLossModel(fit["tx_power"], fit["exponent"])

    rssi_at_3m = env.range_to_rssi(3.0)
    err_before = abs(assumed.rssi_to_range(rssi_at_3m) - 3.0)
    err_after = abs(trained.rssi_to_range(rssi_at_3m) - 3.0)
    assert err_after < 0.01 < err_before


def test_trainer_available_via_registry():
    registry.load_plugins()
    assert "path_loss" in registry.available("trainer")
    assert "detector_finetune" in registry.available("trainer")
