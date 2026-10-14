"""Small reproducible classification problem shared by the demos.

Architecture: seeded 2D samples -> logistic predictions -> measured loss/accuracy
and a PNG probability field. Uses only the Python standard library.
Main callers: the runnable examples; no generated metric stands in for training.
"""
import math
import random
import struct
import zlib


def make_data(seed, count):
    """Generate seeded features, an explicit bias input and noisy binary labels.

    Main callers: numeric training demos. No external dataset is downloaded.
    """
    rng = random.Random(seed)
    rows = []
    for _ in range(count):
        x, y = rng.gauss(0, 1.1), rng.gauss(0, 1.1)
        label = int(1.35 * x - 0.85 * y + rng.gauss(0, 0.75) > 0)
        rows.append(([x, y, 1.0], label))
    return rows


def probability(weights, features):
    logit = max(-40, min(40, sum(w * x for w, x in zip(weights, features))))
    return 1 / (1 + math.exp(-logit))


def measure(weights, rows):
    """Calculate mean cross-entropy, accuracy and probabilities on actual rows.

    Main callers: each logistic training loop and held-out evaluation.
    """
    probabilities = [probability(weights, features) for features, _ in rows]
    loss = -sum(label * math.log(max(p, 1e-12)) + (1 - label) * math.log(max(1 - p, 1e-12))
                for p, (_, label) in zip(probabilities, rows)) / len(rows)
    accuracy = sum(int(p >= 0.5) == label for p, (_, label) in zip(probabilities, rows)) / len(rows)
    return loss, accuracy, probabilities


def write_probability_image(path, weights, width=160, height=120):
    """Encode a small numeric probability heatmap as PNG without image packages.

    Main callers: seed_demo and artifact_pipeline. Pixels represent actual model output on a fixed 2D grid.
    This is a diagnostic scientific image, not a decorative generated asset.
    """
    rows = []
    for row in range(height):
        pixels = bytearray([0])
        for col in range(width):
            p = probability(weights, [(col / (width - 1) - .5) * 6, (.5 - row / (height - 1)) * 6, 1])
            pixels.extend((int(28 + p * 51), int(53 + p * 174), int(102 + p * 78)))
        rows.append(bytes(pixels))
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b"")
    path.write_bytes(data)


def train_step(weights, rows, learning_rate):
    """Compute one full-batch logistic gradient update from actual examples.

    Main callers: seed_demo, shared_training and artifact_pipeline.
    """
    gradient = [0.0] * len(weights)
    for features, label in rows:
        error = probability(weights, features) - label
        for index, feature in enumerate(features):
            gradient[index] += error * feature / len(rows)
    return [w - learning_rate * g for w, g in zip(weights, gradient)]
