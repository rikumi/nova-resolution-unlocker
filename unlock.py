import argparse
import base64
import concurrent.futures
import datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET


PACKAGE = "com.RoamingStar.StellaSora"
ACTIVITY = PACKAGE + "/com.tech.sdk.bridge.TechSDKUnityPlayerActivity"
PREFS = "/data/user/0/" + PACKAGE + "/shared_prefs/" + PACKAGE + ".v2.playerprefs.xml"
WIDTH = "Screenmanager%20Resolution%20Width"
HEIGHT = "Screenmanager%20Resolution%20Height"
LIMIT = 4 * 1024 * 1024


def parse_prefs(data):
    if len(data) > LIMIT or b"<!doctype" in data.lower() or b"<!entity" in data.lower():
        raise ValueError("配置文件大小或 XML 格式不受支持")
    root = ET.fromstring(data)
    if root.tag != "map":
        raise ValueError("不是有效的游戏偏好配置")
    entries = {entry.attrib["name"]: entry for entry in root}
    if len(entries) != len(root):
        raise ValueError("配置文件存在重复字段")
    settings = json.loads(urllib.parse.unquote(entries["RENDERING_SETTING"].text or ""))
    return entries, settings


def patch_prefs(data, width, height):
    entries, settings = parse_prefs(data)
    if settings.get("RENDERING_SETTING_VER") != 9:
        raise ValueError("不支持此画质配置版本；当前仅验证过版本 9")
    if settings.get("RENDERING_SETTING_TIER") != 90:
        raise ValueError("请先在游戏中将画质设为自定义，再运行脚本")
    if (
        settings.get("RESOLUTION_SCALE_QUALITY_LEVEL") == 3
        and entries[WIDTH].attrib.get("value") == str(width)
        and entries[HEIGHT].attrib.get("value") == str(height)
    ):
        return data
    decoded = urllib.parse.unquote(entries["RENDERING_SETTING"].text)
    decoded, count = re.subn(
        r'("RESOLUTION_SCALE_QUALITY_LEVEL"\s*:\s*)-?\d+',
        lambda match: match[1] + "3",
        decoded,
    )
    expected = dict(settings)
    expected["RESOLUTION_SCALE_QUALITY_LEVEL"] = 3
    if count != 1 or json.loads(decoded) != expected:
        raise ValueError("无法安全修改分辨率档位")
    text, count = re.subn(
        r'(<string\s+name="RENDERING_SETTING">)[^<]*(</string>)',
        lambda match: match[1] + urllib.parse.quote(decoded, safe="") + match[2],
        data.decode("utf-8"),
    )
    if count != 1:
        raise ValueError("画质字段格式不受支持")
    for key, value in ((WIDTH, width), (HEIGHT, height)):
        pattern = r'(<int\s+name="' + re.escape(key) + r'"\s+value=")-?\d+("\s*/>)'
        text, count = re.subn(pattern, lambda match: match[1] + str(value) + match[2], text)
        if count != 1:
            raise ValueError("无法安全修改字段：" + key)
    patched = text.encode("utf-8")
    updated, actual = parse_prefs(patched)
    allowed = {WIDTH, HEIGHT, "RENDERING_SETTING"}
    if (
        set(entries) != set(updated)
        or actual != expected
        or updated[WIDTH].attrib.get("value") != str(width)
        or updated[HEIGHT].attrib.get("value") != str(height)
        or any(ET.tostring(entries[key]) != ET.tostring(updated[key]) for key in entries if key not in allowed)
    ):
        raise ValueError("修改涉及非目标字段，已中止")
    return patched


class Device:
    def __init__(self, serial):
        self.serial = serial
        self.adb = ["adb", "-s", serial]

    def run(self, *args, data=None, timeout=45, check=True):
        result = subprocess.run(self.adb + list(args), input=data, capture_output=True, timeout=timeout)
        if check and result.returncode:
            message = result.stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(message or "ADB 命令失败")
        return result

    def shell(self, command, data=None):
        return self.run("shell", "-T", command, data=data).stdout

    def root(self, command, data=None):
        return self.shell("su -c " + shlex.quote(command), data=data)

    def read(self):
        return self.root("cat " + shlex.quote(PREFS))

    def write(self, data):
        self.root("dd of=" + shlex.quote(PREFS) + " bs=1 count=" + str(len(data)) + " 2>/dev/null", data=data)

    def prepare(self):
        if self.root("id -u").strip() != b"0":
            raise RuntimeError("需要可通过 adb shell su 获取的 root 权限")
        hardware = self.shell("getprop ro.serialno").decode().strip()
        if not hardware:
            hardware = self.shell("getprop ro.boot.serialno").decode().strip()
        if not hardware:
            raise RuntimeError("无法取得设备序列号，不能安全创建可恢复备份")
        model = self.shell("getprop ro.product.model").decode().strip()
        package = self.shell("dumpsys package " + PACKAGE).decode("utf-8", "replace")
        version = re.search(r"\bversionName=([^\s]+)", package)
        if not version or version[1] != "2.0.0":
            raise RuntimeError("当前仅支持已验证的国服星塔旅人 2.0.0")
        size = re.search(r"Physical size:\s*(\d+)x(\d+)", self.shell("wm size").decode())
        if not size:
            raise RuntimeError("无法读取物理屏幕尺寸")
        width, height = sorted((int(size[1]), int(size[2])), reverse=True)
        if not 1 <= height <= width <= 16384:
            raise RuntimeError("屏幕尺寸异常")
        return {"hardware": hardware, "model": model, "width": width, "height": height}

    def stop(self):
        self.shell("am force-stop " + PACKAGE)
        result = self.run("shell", "pidof", PACKAGE, check=False)
        if result.returncode != 1 or result.stdout.strip() or result.stderr.strip():
            raise RuntimeError("无法确认游戏已停止，未写入配置")

    def start(self):
        output = self.shell("am start -W -n " + ACTIVITY).decode("utf-8", "replace")
        if "Status: ok" not in output:
            raise RuntimeError("配置已保存，但游戏启动未获确认，请手动启动")

    def verify_output(self, width, height):
        time.sleep(10)
        entries, settings = parse_prefs(self.read())
        if (
            settings.get("RESOLUTION_SCALE_QUALITY_LEVEL") != 3
            or entries[WIDTH].attrib.get("value") != str(width)
            or entries[HEIGHT].attrib.get("value") != str(height)
        ):
            raise RuntimeError("游戏启动后覆盖了配置，请检查游戏内画质设置")
        text = self.run("shell", "dumpsys", "SurfaceFlinger").stdout.decode("utf-8", "replace")
        for block in re.split(r"(?m)(?=^\s*(?:Layer \[|\+ Layer ))", text):
            heading = block.splitlines()[0] if block.splitlines() else ""
            if PACKAGE not in heading or "SurfaceView" not in heading or "BLAST" not in heading:
                continue
            for pattern in (r"geomBufferSize=\[\s*0\s+0\s+(\d+)\s+(\d+)\s*\]", r"activeBuffer=\[\s*(\d+)x(\d+)"):
                match = re.search(pattern, block)
                if match and (int(match[1]), int(match[2])) == (width, height):
                    return True
        return False


def save_backup(directory, info, data):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    hardware = re.sub(r"[^A-Za-z0-9_-]", "_", info["hardware"])
    path = directory / (hardware + "-" + stamp + ".json")
    payload = {"format": 1, "package": PACKAGE, "hardware": info["hardware"], "model": info["model"], "prefs_b64": base64.b64encode(data).decode("ascii")}
    descriptor = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    if base64.b64decode(json.loads(path.read_text(encoding="utf-8"))["prefs_b64"], validate=True) != data:
        raise RuntimeError("备份校验失败，未修改游戏配置")
    return path


def restore_data(path, info):
    with path.open("rb") as stream:
        raw = stream.read(LIMIT * 8 + 1)
    if len(raw) > LIMIT * 8:
        raise ValueError("备份文件过大")
    payload = json.loads(raw)
    if payload.get("format") != 1 or payload.get("package") != PACKAGE or payload.get("hardware") != info["hardware"]:
        raise ValueError("备份格式错误或不属于此设备，拒绝恢复")
    data = base64.b64decode(payload["prefs_b64"], validate=True)
    parse_prefs(data)
    return data


def process(serial, args):
    device = Device(serial)
    backup = None
    try:
        info = device.prepare()
        width, height = info["width"], info["height"]
        original = device.read()
        target = restore_data(args.restore, info) if args.restore else patch_prefs(original, width, height)
        if target == original:
            print("[" + info["model"] + "] 配置已符合目标，无需修改或重启游戏", flush=True)
            return True
        device.stop()
        original = device.read()
        if len(original) > LIMIT:
            raise ValueError("当前配置文件过大，已中止")
        target = restore_data(args.restore, info) if args.restore else patch_prefs(original, width, height)
        backup = save_backup(args.backup_dir, info, original)
        print("[" + info["model"] + "] 备份：" + str(backup), flush=True)
        try:
            device.write(target)
            if device.read() != target:
                raise RuntimeError("写入后校验失败")
        except BaseException:
            try:
                device.write(original)
                if device.read() != original:
                    raise RuntimeError("恢复校验失败")
                print("[" + info["model"] + "] 已恢复本次修改前的配置", flush=True)
            except BaseException:
                print("[" + serial + "] 自动恢复失败；不要启动游戏，请重连后使用 --restore " + str(backup), file=sys.stderr, flush=True)
            raise
        device.start()
        if args.restore:
            print("[" + info["model"] + "] 已恢复备份并启动游戏", flush=True)
        else:
            print("[" + info["model"] + "] 已写入档位 3、" + str(width) + "×" + str(height) + "，并启动游戏", flush=True)
            try:
                confirmed = device.verify_output(width, height)
                message = "已确认原生尺寸输出缓冲区" if confirmed else "缓存保持有效；暂未确认输出尺寸，请进入游戏后检查"
                print("[" + info["model"] + "] " + message, flush=True)
            except Exception as error:
                print("[" + info["model"] + "] 修改已完成，运行验证未通过：" + str(error), file=sys.stderr, flush=True)
                return False
        return True
    except Exception as error:
        print("[" + serial + "] " + str(error), file=sys.stderr, flush=True)
        if backup:
            print("备份仍保留在：" + str(backup), file=sys.stderr, flush=True)
        return False


def main():
    parser = argparse.ArgumentParser(description="通过本地画质缓存解锁星塔旅人原生分辨率（需要 root）")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("-s", "--serial", help="adb devices 中的设备标识")
    selection.add_argument("--all", action="store_true", help="处理所有在线 ADB 设备")
    parser.add_argument("--restore", type=Path, help="恢复指定 JSON 备份，仅支持单台设备")
    parser.add_argument("--backup-dir", type=Path, default=Path.home() / ".nova-resolution-unlocker" / "backups", help="备份目录")
    args = parser.parse_args()
    if args.restore and args.all:
        parser.error("--restore 不能与 --all 同时使用")
    if not shutil.which("adb"):
        parser.error("找不到 adb，请安装 Android Platform Tools 并加入 PATH")
    result = subprocess.run(["adb", "devices"], capture_output=True, text=True, timeout=20, check=True)
    devices = [parts[0] for line in result.stdout.splitlines() if len(parts := line.split()) >= 2 and parts[1] == "device"]
    if args.serial:
        if args.serial not in devices:
            parser.error("指定设备不在线，请先连接或授权无线调试")
        devices = [args.serial]
    elif not devices:
        parser.error("没有在线设备，请先连接并授权 ADB")
    elif len(devices) > 1 and not args.all:
        parser.error("存在多台设备，请用 -s 指定或 --all 批量处理：" + ", ".join(devices))
    if args.all:
        unique = {}
        for serial in devices:
            device = Device(serial)
            hardware = device.shell("getprop ro.serialno").decode().strip()
            if not hardware:
                hardware = device.shell("getprop ro.boot.serialno").decode().strip()
            unique.setdefault(hardware or serial, serial)
        devices = list(unique.values())
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(devices))) as executor:
        results = list(executor.map(lambda serial: process(serial, args), devices))
    return 0 if all(results) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("操作中断；若设备已掉线，请使用打印的备份路径恢复配置", file=sys.stderr)
        sys.exit(130)
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
