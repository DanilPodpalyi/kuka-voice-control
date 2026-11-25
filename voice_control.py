import argparse
import json
import math
import os
import re
import sys
import wave
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import URLError

RATE = 16000
DEFAULT_STAND = Path(__file__).with_name("stand.json")
COMMANDS = {
    "начало работы": "initialize",
    "центр": "center",
    "вниз": "down",
    "вверх": "up",
    "вправо": "right",
    "влево": "left",
    "домой": "home",
    "начальная": "home",
    "открой": "open_grip",
    "открой захват": "open_grip",
    "отпусти": "open_grip",
    "захвати": "close_grip",
    "закрой захват": "close_grip",
    "схвати": "close_grip",
    "красный кубик": "red_cube",
    "завершить сеанс": "end_session",
    "стоп": "end_session",
}
UNITS = "один два три четыре пять шесть семь восемь девять".split()
TEENS = "десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать".split()
TENS = "двадцать тридцать сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто".split()
SPEED_WORDS = {
    **{word: value for value, word in enumerate(UNITS, 1)},
    **{word: value for value, word in enumerate(TEENS, 10)},
    **{word: value * 10 for value, word in enumerate(TENS, 2)},
    **{
        f"{tens} {unit}": tens_value * 10 + unit_value
        for tens_value, tens in enumerate(TENS, 2)
        for unit_value, unit in enumerate(UNITS, 1)
    },
    "сто": 100,
}
SPEED_HELP = "Скажите «Скорость» и целое число от 1 до 100, например «Скорость двадцать пять»."


class InvalidSpeed(ValueError):
    pass


class RecognitionError(Exception):
    pass


def load_stand(path):
    config = json.loads(path.read_text(encoding="utf-8"))

    def is_number(value):
        return type(value) in (int, float) and math.isfinite(value)

    def check_point(point, size):
        if not isinstance(point, list) or len(point) != size or not all(map(is_number, point)):
            raise ValueError(f"Ожидалась точка с {size} числовыми координатами")

    if type(config["port"]) is not int or not 1 <= config["port"] <= 65535:
        raise ValueError("port должен быть целым числом от 1 до 65535")
    if not is_number(config["socket_timeout_seconds"]) or config["socket_timeout_seconds"] <= 0:
        raise ValueError("socket_timeout_seconds должен быть положительным")
    if not is_number(config["approximation_distance_mm"]) or config["approximation_distance_mm"] < 0:
        raise ValueError("approximation_distance_mm должен быть неотрицательным")
    check_point(config["center_mm"], 3)
    for action in ("up", "down", "left", "right"):
        check_point(config["offsets_mm"][action], 3)
    for action in ("home", "red_cube"):
        points = config["paths"][action]
        if not isinstance(points, list) or not points:
            raise ValueError(f"Траектория {action} должна содержать хотя бы одну точку")
        for point in points:
            check_point(point, 6)
    return config


def parse_command(text):
    phrase = " ".join(text.casefold().strip().strip(".,!?:;").split())
    keyword = re.match(r"^скорость\b[\s,:]*", phrase)
    if keyword:
        raw_value = re.sub(r"(?:\s*%|\s+процент(?:а|ов)?)$", "", phrase[keyword.end():])
        value = int(raw_value) if re.fullmatch(r"[0-9]{1,3}", raw_value) else SPEED_WORDS.get(raw_value)
        if value is None or not 1 <= value <= 100:
            raise InvalidSpeed(SPEED_HELP)
        return "speed", value
    return COMMANDS.get(phrase), None


def execute(kuka, action, value, stand):
    if action in ("open_grip", "close_grip", "speed"):
        if action == "speed":
            kuka.set_speed(value)
        elif action == "open_grip":
            kuka.open_grip()
        else:
            kuka.close_grip()
        return

    import numpy as np

    kuka.read_cartessian()
    if action == "initialize":
        print(kuka.global_position)
        return

    current = list(kuka.global_position)
    if action in stand["offsets_mm"]:
        delta = stand["offsets_mm"][action]
        points = [[current[i] + delta[i] for i in range(3)] + current[3:]]
    elif action == "center":
        points = [stand["center_mm"] + current[3:]]
    elif action in stand["paths"]:
        points = stand["paths"][action]
    else:
        raise ValueError(f"Неизвестное действие: {action}")
    kuka.lin_continuous(np.asarray([current, *points]))


def recognize_yandex(pcm):
    key = os.environ.get("YANDEX_API_KEY")
    token = os.environ.get("YANDEX_IAM_TOKEN")
    folder = os.environ.get("YANDEX_FOLDER_ID")
    if bool(key) == bool(token):
        raise RecognitionError("Задайте YANDEX_API_KEY или YANDEX_IAM_TOKEN")
    params = {"lang": "ru-RU", "format": "lpcm", "sampleRateHertz": RATE}
    if not key and folder:
        params["folderId"] = folder
    authorization = f"Api-Key {key}" if key else f"Bearer {token}"
    url = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize?" + urlencode(params)
    request = Request(url, data=pcm, headers={
        "Authorization": authorization,
        "Content-Type": "application/octet-stream",
    })
    try:
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RecognitionError("Yandex SpeechKit недоступен или вернул ошибку") from error
    if not isinstance(result, dict) or not isinstance(result.get("result"), str):
        raise RecognitionError("Некорректный ответ Yandex SpeechKit")
    return result["result"]


def recognize_google(pcm):
    import speech_recognition as sr

    recognizer = sr.Recognizer()
    recognizer.operation_timeout = 15
    audio = sr.AudioData(pcm, RATE, 2)
    try:
        return recognizer.recognize_google(audio, language="ru-RU")
    except sr.UnknownValueError:
        return ""
    except (sr.RequestError, TimeoutError) as error:
        raise RecognitionError("Google Speech Recognition недоступен или вернул ошибку") from error


def read_wav(path):
    with wave.open(str(path), "rb") as source:
        if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, RATE):
            raise ValueError("Требуется WAV mono PCM16, 16 кГц")
        return source.readframes(source.getnframes())


def record(seconds):
    import pyaudio

    audio = pyaudio.PyAudio()
    try:
        stream = audio.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True)
        try:
            count = seconds * RATE
            frames = bytearray()
            for start in range(0, count, 1024):
                frames.extend(stream.read(min(1024, count - start)))
            return bytes(frames)
        finally:
            stream.close()
    finally:
        audio.terminate()


def main():
    parser = argparse.ArgumentParser(description="Голосовое управление манипулятором KUKA")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--wav", type=Path)
    source.add_argument("--mic", action="store_true")
    parser.add_argument("--provider", choices=("yandex", "google"), default="google")
    parser.add_argument("--seconds", type=int, choices=range(1, 11), default=3)
    parser.add_argument("--denoise-model", type=Path)
    parser.add_argument("--robot-host", help="Подключить физический стенд по OpenShowVar")
    parser.add_argument("--robot-port", type=int, help="Переопределить порт из конфигурации")
    parser.add_argument("--stand-config", type=Path, default=DEFAULT_STAND,
                        help="JSON с параметрами стенда (по умолчанию stand.json)")
    args = parser.parse_args()
    if args.denoise_model and args.text is not None:
        parser.error("--denoise-model требует --wav или --mic")
    if args.robot_port is not None and not 1 <= args.robot_port <= 65535:
        parser.error("--robot-port должен быть от 1 до 65535")
    try:
        stand = load_stand(args.stand_config)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(f"Ошибка конфигурации стенда ({args.stand_config}): {error}")

    robot = None
    motion_configured = False
    try:
        denoise = None
        if args.denoise_model:
            from denoise import load_filter

            denoise = load_filter(args.denoise_model)
        recognize = {"google": recognize_google, "yandex": recognize_yandex}[args.provider]
        while True:
            if args.text is not None:
                phrase = args.text
            else:
                if args.mic:
                    input("Enter — записать команду, Ctrl+C — выйти. ")
                    print("Говорите…", flush=True)
                    pcm = record(args.seconds)
                else:
                    pcm = read_wav(args.wav)
                try:
                    phrase = recognize(denoise(pcm) if denoise else pcm)
                except RecognitionError as error:
                    print(f"Ошибка распознавания: {error}", file=sys.stderr)
                    if args.mic:
                        continue
                    return 1

            try:
                action, value = parse_command(phrase)
            except InvalidSpeed as error:
                print(error, file=sys.stderr)
                if args.mic:
                    continue
                return 2

            if action == "end_session":
                print("Сеанс завершён.")
                break
            if not phrase.strip():
                print("Речь не распознана. Повторите команду.")
            elif action is None:
                print(f"Неизвестная команда: {phrase!r}.")
            elif not args.robot_host:
                label = f"скорость {value}%" if action == "speed" else action
                print(f"{phrase!r} -> {label} (текст команды, без исполнения)")
            else:
                if robot is None:
                    from kuka import Kuka
                    from py_openshowvar import openshowvar

                    robot = openshowvar(args.robot_host, args.robot_port or stand["port"])
                    robot.sock.settimeout(stand["socket_timeout_seconds"])
                    kuka = Kuka(robot)
                is_motion = action in stand["offsets_mm"] or action in stand["paths"] or action == "center"
                if is_motion and not motion_configured:
                    distance = str(stand["approximation_distance_mm"])
                    if robot.write("$APO.CDIS", distance, False) is None:
                        raise OSError("Контроллер отклонил настройку $APO.CDIS")
                    motion_configured = True
                execute(kuka, action, value, stand)
                if action == "speed":
                    print(f"Контроллер подтвердил завершение команды «Скорость {value}%».")
            if not args.mic:
                break
    except (KeyboardInterrupt, EOFError):
        print("Сеанс завершён.")
    except (OSError, ValueError, ImportError) as error:
        print(f"Сеанс прерван: {error}", file=sys.stderr)
        return 1
    finally:
        if robot is not None:
            robot.close()


if __name__ == "__main__":
    raise SystemExit(main())
