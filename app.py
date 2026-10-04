import subprocess
import threading
import time
import re
import os
import queue
import glob
from flask import Flask, render_template, jsonify, request, Response
app = Flask(__name__)
ffmpeg_process = None
streaming_active = False
current_device = "/dev/video0"
stream_stats = {
    "fps": 0.0, "bitrate": "0 kbits/s", "frame": 0,
    "time": "00:00:00", "speed": "1x", "dropped": 0,
    "raw_lines": [],
}
stats_lock = threading.Lock()
clients = []
clients_lock = threading.Lock()
controls_cache = {}
controls_lock = threading.Lock()
def _run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        class R: returncode = 1; stdout = ""; stderr = str(e)
        return R()
def list_video_devices():
    devs = []
    try:
        r = _run(["v4l2-ctl", "--list-devices"])
        if r.returncode == 0 and r.stdout:
            blocks = re.split(r'\n\s*\n', r.stdout.strip())
            for block in blocks:
                lines = [l.strip() for l in block.splitlines() if l.strip()]
                if not lines:
                    continue
                name = lines[0].rstrip(':')
                for line in lines[1:]:
                    if line.startswith("/dev/video"):
                        devs.append({"path": line, "name": name})
        if not devs:
            for p in sorted(glob.glob("/dev/video*")):
                devs.append({"path": p, "name": os.path.basename(p)})
    except Exception:
        pass
    return devs
def list_formats(device):
    r = _run(
        ["ffmpeg", "-hide_banner", "-f", "v4l2", "-list_formats", "1", "-i", device],
        timeout=8
    )
    text = (r.stderr or "") + (r.stdout or "")
    line_re = re.compile(
        r"\[video4linux2.*?\]\s+(Raw|Compressed)\s*:\s*(\S+)\s*:\s*(.+?)\s*:\s*(.+?)\s*$"
    )
    range_re = re.compile(r"\{\s*(\d+)\s*-\s*(\d+)\s*,\s*(\d+)\s*\}\s*x\s*\{\s*(\d+)\s*-\s*(\d+)\s*,\s*(\d+)\s*\}")
    discrete_re = re.compile(r"\b(\d+)x(\d+)\b")
    formats = []
    for line in text.splitlines():
        m = line_re.search(line)
        if not m:
            continue
        kind, fmt_name, descr, sizes_part = m.groups()
        fmt_name = fmt_name.strip()
        if fmt_name.lower() == "unsupported":
            continue
        entry = {
            "fourcc": None,
            "fmt": fmt_name,
            "kind": kind,
            "description": descr.strip(),
            "sizes": [],
            "stepwise": None,
        }
        rm = range_re.search(sizes_part)
        if rm:
            min_w, max_w, step_w, min_h, max_h, step_h = map(int, rm.groups())
            entry["stepwise"] = {
                "kind": "stepwise",
                "min_w": min_w, "min_h": min_h,
                "max_w": max_w, "max_h": max_h,
                "step_w": step_w, "step_h": step_h,
                "fps": [5.0, 10.0, 15.0, 20.0, 25.0, 30.0],
                "fps_min": None, "fps_max": None,
            }
        else:
            for w, h in discrete_re.findall(sizes_part):
                entry["sizes"].append({
                    "kind": "discrete",
                    "width": int(w), "height": int(h),
                    "fps": [10.0, 15.0, 20.0, 25.0, 30.0],
                })
        formats.append(entry)
    fourcc_map = {
        "yuv420p": "YU12",
        "yuyv422": "YUYV",
        "rgb24":   "RGB3",
        "bgr24":   "BGR3",
        "uyvy422": "UYVY",
        "nv12":    "NV12",
        "mjpeg":   "MJPG",
        "h264":    "H264",
    }
    for f in formats:
        f["fourcc"] = fourcc_map.get(f["fmt"], f["fmt"])
    return {"formats": formats}
CTRL_LINE = re.compile(
    r'^\s*(\w+)\s+(0x[0-9a-fA-F]+)\s+\((\w+)\)\s*:\s*(.*)$'
)
MENU_OPT = re.compile(r'^\s*(\d+):\s*(.+?)\s*$')
def parse_controls(device):
    r = _run(["v4l2-ctl", "-d", device, "-L"], timeout=8)
    if r.returncode != 0:
        return {"error": r.stderr.strip() or "v4l2-ctl error"}
    lines = r.stdout.splitlines()
    controls = []
    current = None
    for line in lines:
        m = CTRL_LINE.match(line)
        if m:
            current = {
                "name": m.group(1),
                "id": m.group(2),
                "type": m.group(3),
                "options": [],
                "min": None, "max": None, "step": None,
                "default": None, "value": None,
                "flags": [],
                "raw": m.group(4),
            }
            params = m.group(4)
            for key, val in re.findall(r'(\w+)=([^\s]+)', params):
                if key in ("min", "max", "step", "default", "value"):
                    try:
                        current[key] = int(val)
                    except ValueError:
                        current[key] = val
                elif key == "flags":
                    current["flags"].append(val)
            fm = re.search(r'flags=([^\s]+)', params)
            if fm:
                current["flags"] = fm.group(1).split(',')
            controls.append(current)
            continue
        if current is not None and current["type"] in ("menu", "intmenu"):
            mo = MENU_OPT.match(line)
            if mo:
                val = int(mo.group(1))
                text = mo.group(2).strip()
                if current["type"] == "intmenu":
                    vm = re.search(r'\(([^)]+)\)\s*$', text)
                    actual = text
                    if vm:
                        actual = vm.group(1).strip()
                        try:
                            actual = int(actual, 0)
                        except ValueError:
                            pass
                    current["options"].append({"value": val, "label": text, "actual": actual})
                else:
                    current["options"].append({"value": val, "label": text})
    for c in controls:
        if c["type"] == "menu" and c.get("value") is not None:
            pass
    with controls_lock:
        controls_cache[device] = controls
    return {"controls": controls}
def get_cached_controls(device, force=False):
    with controls_lock:
        if not force and device in controls_cache:
            return controls_cache[device]
    res = parse_controls(device)
    if "error" in res:
        return []
    return res["controls"]
def set_control(device, name, value):
    cmd = ["v4l2-ctl", "-d", device, "-c", f"{name}={value}"]
    print(f"[SET] cmd={' '.join(cmd)}", flush=True)
    r = _run(cmd, timeout=5)
    print(f"[SET] rc={r.returncode} stdout={r.stdout!r} stderr={r.stderr!r}", flush=True)
    if r.returncode != 0:
        return {"error": r.stderr.strip() or "set error"}
    return {"status": "ok", "name": name, "value": value}
def reset_all_controls(device):
    ctrls = get_cached_controls(device, force=True)
    errors = []
    for c in ctrls:
        if c["type"] == "button":
            continue
        if c.get("default") is None:
            continue
        val = c["default"]
        if c["type"] in ("menu", "intmenu") and c.get("options"):
            pass
        r = set_control(device, c["name"], val)
        if "error" in r:
            errors.append(f"{c['name']}: {r['error']}")
    return {"status": "ok", "errors": errors}
def reset_one_control(device, name):
    ctrls = get_cached_controls(device)
    for c in ctrls:
        if c["name"] == name and c.get("default") is not None:
            return set_control(device, name, c["default"])
    return {"error": "control not found or has no default"}
def step_control(device, name, direction, step=None):
    ctrls = get_cached_controls(device)
    c = next((x for x in ctrls if x["name"] == name), None)
    if not c:
        return {"error": "control not found"}
    cur = c.get("value")
    if cur is None:
        return {"error": "control has no current value"}
    if c["type"] == "bool":
        new = 0 if cur else 1
    elif c["type"] in ("menu", "intmenu"):
        new = cur + direction
        if c.get("min") is not None and new < c["min"]:
            new = c["min"]
        if c.get("max") is not None and new > c["max"]:
            new = c["max"]
    else:
        st = step if step is not None else (c.get("step") or 1)
        new = cur + direction * st
        if c.get("min") is not None and new < c["min"]:
            new = c["min"]
        if c.get("max") is not None and new > c["max"]:
            new = c["max"]
    return set_control(device, name, new)
def build_ffmpeg_command(options):
    cmd = ["ffmpeg", "-hide_banner"]
    device = options.get("device", "/dev/video0")
    input_format = options.get("input_format") or None
    resolution = options.get("resolution") or None
    framerate = options.get("framerate") or None
    if input_format:
        cmd += ["-input_format", input_format]
    if resolution and ".." not in resolution:
        cmd += ["-video_size", resolution]
    if framerate:
        cmd += ["-framerate", str(framerate)]
    cmd += ["-f", "v4l2", "-i", device]
    vcodec = options.get("vcodec", "libx264")
    cmd += ["-vcodec", vcodec]
    if vcodec in ("libx264", "libx265"):
        preset = options.get("preset")
        if preset:
            cmd += ["-preset", preset]
        gop = options.get("gop")
        if gop:
            cmd += ["-g", str(gop)]
        keyint_min = options.get("keyint_min")
        if keyint_min:
            cmd += ["-keyint_min", str(keyint_min)]
        sc_threshold = options.get("sc_threshold")
        if sc_threshold is not None and sc_threshold != "":
            cmd += ["-sc_threshold", str(sc_threshold)]
        bf = options.get("bf")
        if bf is not None and bf != "":
            cmd += ["-bf", str(bf)]
        qp_min = options.get("qp_min")
        qp_max = options.get("qp_max")
        if qp_min is not None and qp_min != "":
            cmd += ["-qpmin", str(qp_min)]
        if qp_max is not None and qp_max != "":
            cmd += ["-qpmax", str(qp_max)]
        profile = options.get("profile")
        if profile:
            cmd += ["-profile:v", profile]
        level = options.get("level")
        if level:
            cmd += ["-level", str(level)]
        if options.get("zerolatency", True):
            cmd += ["-tune", "zerolatency"]
        idr = options.get("idr_period")
        if idr:
            cmd += ["-x264-params", f"keyint={idr}:min-keyint={idr}:scenecut=0:open-gop=0"]
    if options.get("bitrate"):
        cmd += ["-b:v", options["bitrate"]]
        cmd += ["-maxrate", options["bitrate"]]
        cmd += ["-bufsize", options["bitrate"]]
    cmd += ["-pix_fmt", "yuv420p"]
    cmd += [
        "-f", "mpegts",
        "-mpegts_flags", "+resend_headers",
        "-"
    ]
    return cmd
def read_ffmpeg_stats(process):
    global stream_stats
    fps_p = re.compile(r'fps=\s*([\d.]+)')
    br_p = re.compile(r'bitrate=\s*([\d.]+)\s*kbits/s')
    fr_p = re.compile(r'frame=\s*(\d+)')
    tm_p = re.compile(r'time=([\d:.]+)')
    sp_p = re.compile(r'speed=\s*([\d.]+x)')
    dr_p = re.compile(r'drop=\s*(\d+)')
    buf = bytearray()
    while process.poll() is None:
        ch = process.stderr.read(1)
        if not ch:
            break
        if ch in (b'\r', b'\n'):
            if not buf:
                continue
            line = buf.decode('utf-8', errors='replace')
            buf.clear()
            with stats_lock:
                m = fps_p.search(line)
                if m: stream_stats["fps"] = float(m.group(1))
                m = br_p.search(line)
                if m: stream_stats["bitrate"] = f"{m.group(1)} kbits/s"
                m = fr_p.search(line)
                if m: stream_stats["frame"] = int(m.group(1))
                m = tm_p.search(line)
                if m: stream_stats["time"] = m.group(1)
                m = sp_p.search(line)
                if m: stream_stats["speed"] = m.group(1)
                m = dr_p.search(line)
                if m: stream_stats["dropped"] = int(m.group(1))
                if line.strip():
                    stream_stats["raw_lines"].append(line.rstrip())
                    stream_stats["raw_lines"] = stream_stats["raw_lines"][-20:]
        else:
            buf.extend(ch)
def broadcaster(process):
    CHUNK = 8192
    try:
        while process.poll() is None:
            data = process.stdout.read(CHUNK)
            if not data:
                break
            with clients_lock:
                dead = []
                for q in clients:
                    try:
                        q.put_nowait(data)
                    except queue.Full:
                        dead.append(q)
                for q in dead:
                    clients.remove(q)
    except Exception:
        pass
    finally:
        with clients_lock:
            for q in clients:
                try:
                    q.put_nowait(None)
                except Exception:
                    pass
            clients.clear()
def start_ffmpeg_stream(options):
    global ffmpeg_process, streaming_active, stream_stats, current_device
    if ffmpeg_process is not None and ffmpeg_process.poll() is None:
        return {"error": "Stream already running"}
    current_device = options.get("device", current_device)
    cmd = build_ffmpeg_command(options)
    try:
        ffmpeg_process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0
        )
        streaming_active = True
        with stats_lock:
            stream_stats = {
                "fps": 0.0, "bitrate": "0 kbits/s", "frame": 0,
                "time": "00:00:00", "speed": "1x", "dropped": 0,
                "raw_lines": [],
            }
        threading.Thread(target=read_ffmpeg_stats,
                         args=(ffmpeg_process,), daemon=True).start()
        threading.Thread(target=broadcaster,
                         args=(ffmpeg_process,), daemon=True).start()
        return {"status": "started", "command": " ".join(cmd)}
    except FileNotFoundError:
        return {"error": "FFmpeg not found"}
    except Exception as e:
        return {"error": str(e)}
def stop_ffmpeg_stream():
    global ffmpeg_process, streaming_active
    if ffmpeg_process is not None:
        try:
            ffmpeg_process.terminate()
            ffmpeg_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ffmpeg_process.kill()
        except Exception:
            pass
        ffmpeg_process = None
    streaming_active = False
    return {"status": "stopped"}
_prev_cpu = {"total": 0, "idle": 0}
_cpu_lock = threading.Lock()
def cpu_percent():
    global _prev_cpu
    try:
        with open("/proc/stat", "r") as f:
            line = f.readline()
        parts = line.split()
        if parts[0] != "cpu":
            return {"usage": 0.0, "load1": 0.0, "load5": 0.0, "load15": 0.0}
        values = list(map(int, parts[1:]))
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        total = sum(values)
        with _cpu_lock:
            prev_total = _prev_cpu["total"]
            prev_idle = _prev_cpu["idle"]
            _prev_cpu["total"] = total
            _prev_cpu["idle"] = idle
        dt = total - prev_total
        di = idle - prev_idle
        usage = 0.0
        if dt > 0:
            usage = max(0.0, min(100.0, 100.0 * (1.0 - di / dt)))
        load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
        ncpu = os.cpu_count() or 1
        return {
            "usage": round(usage, 1),
            "load1": round(load[0], 2),
            "load5": round(load[1], 2),
            "load15": round(load[2], 2),
            "cores": ncpu,
        }
    except Exception as e:
        return {"usage": 0.0, "load1": 0, "load5": 0, "load15": 0, "error": str(e)}
@app.route('/')
def index():
    return render_template('index.html')
@app.route('/api/devices')
def api_devices():
    return jsonify({"devices": list_video_devices(), "current": current_device})
@app.route('/api/device/formats')
def api_formats():
    dev = request.args.get("device", current_device)
    return jsonify(list_formats(dev))
@app.route('/api/controls')
def api_controls():
    dev = request.args.get("device", current_device)
    force = request.args.get("force") == "1"
    ctrls = get_cached_controls(dev, force=force)
    return jsonify({"device": dev, "controls": ctrls})
@app.route('/api/controls/set', methods=['POST'])
def api_set_control():
    data = request.get_json() or {}
    dev = data.get("device", current_device)
    control = data.get("control")
    value = data.get("value")
    if not control or value is None:
        return jsonify({"error": "control/value required"}), 400
    return jsonify(set_control(dev, control, value))
@app.route('/api/controls/reset', methods=['POST'])
def api_reset_control():
    data = request.get_json() or {}
    dev = data.get("device", current_device)
    if data.get("all"):
        return jsonify(reset_all_controls(dev))
    name = data.get("control")
    if not name:
        return jsonify({"error": "control required"}), 400
    return jsonify(reset_one_control(dev, name))
@app.route('/api/controls/step', methods=['POST'])
def api_step_control():
    data = request.get_json() or {}
    dev = data.get("device", current_device)
    name = data.get("control")
    direction = int(data.get("direction", 1))
    step = data.get("step")
    if step is not None:
        step = int(step)
    if not name:
        return jsonify({"error": "control required"}), 400
    return jsonify(step_control(dev, name, direction, step))
@app.route('/api/stream/start', methods=['POST'])
def api_start_stream():
    return jsonify(start_ffmpeg_stream(request.get_json() or {}))
@app.route('/api/stream/stop', methods=['POST'])
def api_stop_stream():
    return jsonify(stop_ffmpeg_stream())
@app.route('/api/stream/status')
def api_stream_status():
    global ffmpeg_process, stream_stats
    is_running = ffmpeg_process is not None and ffmpeg_process.poll() is None
    with stats_lock:
        stats = dict(stream_stats)
        stats["raw_lines"] = list(stream_stats.get("raw_lines", []))
    return jsonify({"active": is_running, "stats": stats})
@app.route('/api/cpu')
def api_cpu():
    return jsonify(cpu_percent())
@app.route('/stream.ts')
def stream_ts():
    if ffmpeg_process is None or ffmpeg_process.poll() is not None:
        return "Stream not running", 404
    q = queue.Queue(maxsize=200)
    with clients_lock:
        clients.append(q)
    def gen():
        try:
            while True:
                try:
                    chunk = q.get(timeout=15)
                except queue.Empty:
                    break
                if chunk is None:
                    break
                yield chunk
        finally:
            with clients_lock:
                if q in clients:
                    clients.remove(q)
    return Response(
        gen(),
        mimetype='video/mp2t',
        headers={
            'Cache-Control': 'no-cache, no-store',
            'Pragma': 'no-cache',
            'Connection': 'close',
        }
    )
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=True, threaded=True, use_reloader=True)