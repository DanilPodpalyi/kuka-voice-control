import argparse
import wave
from pathlib import Path

from voice_control import RATE, read_wav


def load_filter(path):
    suffix = path.suffix.lower()
    if suffix not in (".onnx", ".xml"):
        raise ValueError("Поддерживаются модели NSNet2 в .onnx и .xml")
    if not path.is_file():
        raise FileNotFoundError(f"Файл модели не найден: {path}")
    if suffix == ".xml" and not path.with_suffix(".bin").is_file():
        raise FileNotFoundError("Укажите существующую пару файлов NSNet2 .xml и .bin")

    import numpy as np

    from .model import DeepNoiseSuppression

    model = DeepNoiseSuppression(str(path), "CPU")
    block_size = int(model.chunk * RATE)
    hop = 160

    def filter_pcm(pcm):
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        result = np.zeros(len(samples) + block_size + hop, dtype=np.float32)
        for start in range(0, len(samples), block_size):
            block = samples[start:start + block_size]
            block = np.pad(block, (0, block_size - len(block)))
            filtered = model.denoise(block).reshape(-1)
            result[start:start + len(filtered)] += filtered
        return np.clip(result[:len(samples)] * 32768, -32768, 32767).astype("<i2").tobytes()

    return filter_pcm


def main():
    parser = argparse.ArgumentParser(description="Шумоподавление NSNet2 для WAV")
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error("Входной и выходной WAV должны различаться")
    pcm = load_filter(args.model)(read_wav(args.input))
    with wave.open(str(args.output), "wb") as output:
        output.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        output.writeframes(pcm)
