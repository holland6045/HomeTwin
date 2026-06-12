#!/usr/bin/env bash
# Export RT-DETRv2 (Apache-2.0, COCO) to ONNX for the `rtdetr` detector.
# Run anywhere with internet + python; copy the .onnx next to your config.
set -euo pipefail
MODEL="${MODEL:-PekingU/rtdetr_v2_r18vd}"   # r18vd ~ realtime CPU; r50vd = better
OUT="${OUT:-rtdetr_onnx}"
pip install --quiet "optimum[exporters]" torch --index-url https://download.pytorch.org/whl/cpu \
    --extra-index-url https://pypi.org/simple
optimum-cli export onnx --model "$MODEL" --task object-detection "$OUT"
echo "config: detector: {type: rtdetr, model_path: $OUT/model.onnx}"
