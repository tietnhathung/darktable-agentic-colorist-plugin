from pathlib import Path

import cv2
import numpy as np


def analyze_image(path: Path) -> dict:
    # imdecode supports Windows paths containing characters outside the ANSI code page.
    data = np.frombuffer(path.read_bytes(), dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None
    if image is None:
        raise ValueError(f"Cannot decode preview: {path}")
    value = image.max(axis=2)
    blue, green, red = image.mean(axis=(0, 1)).tolist()
    histogram = np.bincount(value.ravel(), minlength=256)
    return {
        "mean_value": float(value.mean()),
        "highlights_clipped_pct": 100 * float((value > 250).mean()),
        "shadows_crushed_pct": 100 * float((value < 8).mean()),
        "rgb_mean": {"r": red, "g": green, "b": blue},
        "r_over_b": red / blue if blue else None,
        "g_over_b": green / blue if blue else None,
        "value_histogram": histogram.tolist(),
    }
