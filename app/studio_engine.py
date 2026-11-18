#!/usr/bin/env python3
"""
InfoShot - Yerel Shot Reçetesi ve Otomasyon Motoru
Higgsfield indirmelerini otomatik yakalar, sahnelerle eşleştirir ve projeleri senkronize eder.
"""

import os
import sys
import json
import time
import shutil
import re
import mimetypes
import threading
import struct
import subprocess
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

# Dizin Yolları
APP_DIR = os.path.dirname(os.path.abspath(__file__))
STUDIO_DIR = os.path.dirname(APP_DIR)
PROJECTS_DIR = os.path.join(STUDIO_DIR, "projeler")
DOWNLOADS_DIR = os.path.expanduser("~/Downloads")
DESKTOP_DIR = os.path.expanduser("~/Desktop")
WATCH_DIRS = [DOWNLOADS_DIR, DESKTOP_DIR]

PORT = 8765

# Global Durum (State)
state_lock = threading.Lock()
engine_state = {
    "active_project_id": None,
    "active_shot_id": None,
    "last_captured": None,
    "downloads_dir": "İndirilenler & Masaüstü",
    "watcher_active": True,
    "project_version": int(time.time() * 1000)
}

def slugify(text):
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'[-\s]+', '_', text).strip('-_')

project_file_lock = threading.Lock()

def clean_id(val):
    if not val:
        return ""
    return re.sub(r'[^\w\-]', '', str(val))

def is_safe_project_path(path):
    if not path:
        return False
    try:
        resolved = os.path.realpath(path)
        base = os.path.realpath(PROJECTS_DIR)
        return (resolved == base or resolved.startswith(base + os.sep)) and not (".." in str(path))
    except Exception:
        return False

def get_next_shot_id(shots):
    max_num = 0
    for s in (shots or []):
        sid = s.get("id", "")
        parts = sid.split("_")
        if len(parts) >= 2 and parts[1].isdigit():
            max_num = max(max_num, int(parts[1]))
    return f"shot_{max_num + 1:02d}"

def get_next_take_id(takes):
    max_num = 0
    for t in (takes or []):
        tid = t.get("id", "")
        parts = tid.split("_")
        if len(parts) >= 2 and parts[1].isdigit():
            max_num = max(max_num, int(parts[1]))
    return f"take_{max_num + 1:02d}"

def send_to_trash(path):
    if not path or not os.path.exists(path):
        return False
    trash_dir = os.path.expanduser("~/.Trash")
    os.makedirs(trash_dir, exist_ok=True)
    base_name = os.path.basename(path.rstrip('/'))
    target = os.path.join(trash_dir, base_name)
    if os.path.exists(target):
        target = os.path.join(trash_dir, f"{base_name}_{int(time.time())}")
    try:
        shutil.move(path, target)
        print(f"🗑️ [ÇÖP SEPETİNE TAŞINDI] {path} -> {target}")
        return True
    except Exception as e:
        print(f"[HATA] Çöp sepetine taşınamadı: {e}")
        return False

def calculate_default_credits(model, quality, duration, file_type):
    q_lower = (quality or '').lower()
    if file_type == 'image':
        if 'max' in q_lower:
            return 10
        return 5
    elif file_type == 'video':
        dur_sec = 5
        if duration:
            try:
                dur_sec = float(str(duration).replace('s', '').strip())
            except Exception:
                pass
        if dur_sec > 6:
            return 40
        return 20
    return 5

def get_media_metadata(file_path):
    if not file_path or not os.path.exists(file_path):
        return None
    try:
        size_bytes = os.path.getsize(file_path)
        size_str = f"{size_bytes / (1024 * 1024):.1f} MB" if size_bytes >= 1024 * 1024 else f"{size_bytes / 1024:.0f} KB"
        ext = os.path.splitext(file_path)[1].lower()
        fname = os.path.basename(file_path)
        w, h, fps, duration = None, None, None, None
        model = 'Higgsfield AI'
        quality = 'Standard'
        is_hf = fname.startswith('hf_')
        c2pa_detected = None

        if ext in ('.png', '.jpg', '.jpeg', '.webp'):
            if ext == '.png':
                try:
                    with open(file_path, 'rb') as f:
                        sig = f.read(8)
                        if sig == b'\x89PNG\r\n\x1a\n':
                            while True:
                                len_b = f.read(4)
                                if not len_b or len(len_b) < 4:
                                    break
                                clen = struct.unpack('>I', len_b)[0]
                                ctype = f.read(4)
                                if ctype == b'IHDR':
                                    w, h = struct.unpack('>II', f.read(8))
                                    f.seek(clen - 8 + 4, 1)
                                elif ctype in (b'caBX', b'C2PA'):
                                    c2pa_sample = f.read(min(clen, 65536)).lower()
                                    f.seek(clen - min(clen, 65536) + 4, 1)
                                    if b'gpt-image' in c2pa_sample or b'openai' in c2pa_sample or b'sunburst' in c2pa_sample:
                                        c2pa_detected = 'GPT Image 2.5 Sunburst'
                                    elif b'kling' in c2pa_sample:
                                        c2pa_detected = 'Kling AI'
                                    elif b'seedance' in c2pa_sample:
                                        c2pa_detected = 'Seedance'
                                    elif b'wan' in c2pa_sample:
                                        c2pa_detected = 'Wan 2.1'
                                    elif b'veo' in c2pa_sample:
                                        c2pa_detected = 'Veo 2'
                                elif ctype in (b'tEXt', b'iTXt'):
                                    tdata = f.read(clen)
                                    f.seek(4, 1)
                                    if b'hf-job-id' in tdata:
                                        is_hf = True
                                else:
                                    f.seek(clen + 4, 1)
                                if ctype == b'IEND':
                                    break
                except Exception:
                    pass
            elif ext in ('.jpg', '.jpeg'):
                try:
                    with open(file_path, 'rb') as f:
                        head = f.read(2)
                        if head == b'\xff\xd8':
                            b = f.read(1)
                            while b:
                                while b != b'\xff': b = f.read(1)
                                while b == b'\xff': b = f.read(1)
                                if b >= b'\xc0' and b <= b'\xc3':
                                    f.read(3)
                                    h, w = struct.unpack('>HH', f.read(4))
                                    break
                                f.seek(struct.unpack('>H', f.read(2))[0] - 2, 1)
                                b = f.read(1)
                except Exception:
                    pass

            if c2pa_detected:
                model = c2pa_detected
            elif is_hf:
                # Higgsfield Resim Stüdyosu birincil görsel üretim modeli
                model = 'GPT Image 2.5 Sunburst'
            else:
                model = 'Higgsfield AI'

        elif ext in ('.mp4', '.mov', '.webm'):
            try:
                cmd = ['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=width,height,r_frame_rate,duration:format_tags=comment,title,encoder', '-of', 'default=noprint_wrappers=1:nokey=1', file_path]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
                lines = [l.strip() for l in res.stdout.splitlines() if l.strip()]
                if len(lines) >= 2:
                    w, h = int(lines[0]), int(lines[1])
                if len(lines) >= 3 and '/' in lines[2]:
                    num, den = lines[2].split('/')
                    if float(den) > 0: fps = round(float(num) / float(den))
                if len(lines) >= 4 and lines[3].replace('.', '', 1).isdigit():
                    duration = f"{float(lines[3]):.1f}s"
                tags_str = ' '.join(lines[4:]).lower() if len(lines) > 4 else ''
                if 'kling' in tags_str: model = 'Kling AI'
                elif 'seedance' in tags_str: model = 'Seedance'
                elif 'wan' in tags_str: model = 'Wan 2.1'
                elif 'veo' in tags_str: model = 'Veo 2'
                elif is_hf: model = 'Kling AI'
                else: model = 'AI Video'
            except Exception:
                pass

        aspect = ''
        if w and h:
            r = w / h
            if abs(r - 16/9) < 0.05: aspect = '16:9'
            elif abs(r - 9/16) < 0.05: aspect = '9:16'
            elif abs(r - 1.0) < 0.05: aspect = '1:1'
            elif abs(r - 4/5) < 0.05: aspect = '4:5'
            elif abs(r - 2.39) < 0.1: aspect = '2.39:1'
            else: aspect = f"{r:.2f}:1"

            max_dim = max(w, h)
            if max_dim >= 2560:
                quality = 'Max'
            elif max_dim >= 1920:
                quality = 'High'
            elif max_dim >= 1280:
                quality = 'Standard'
            else:
                quality = 'Standard'

        ftype = 'video' if ext in ('.mp4', '.mov', '.webm') else 'image'
        credits_val = calculate_default_credits(model, quality, duration, ftype)

        return {
            'filename': fname,
            'model': model,
            'model_source': model,
            'quality': quality,
            'quality_tier': quality,
            'credits': credits_val,
            'resolution': f"{w}x{h}" if (w and h) else 'Bilinmiyor',
            'aspect_ratio': aspect,
            'size_str': size_str,
            'fps': f"{fps} FPS" if fps else None,
            'duration': duration,
            'file_type': ftype
        }
    except Exception:
        return None

def get_project_path(project_id):
    return os.path.join(PROJECTS_DIR, project_id)

def get_project_json_path(project_id):
    return os.path.join(get_project_path(project_id), "proje.json")

def load_project(project_id):
    proj_id_clean = clean_id(project_id)
    if not proj_id_clean:
        return None
    json_path = get_project_json_path(proj_id_clean)
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Medya künyesini zenginleştir (sadece eksikse veya ilk defa yükleniyorsa)
            proj_dir = get_project_path(proj_id_clean)
            needs_save = False
            for shot in data.get("shots", []):
                shot_dir = os.path.join(proj_dir, clean_id(shot["id"]))
                if shot.get("file") and not shot.get("media_info"):
                    fpath = os.path.join(shot_dir, os.path.basename(shot["file"]))
                    if os.path.exists(fpath):
                        shot["media_info"] = get_media_metadata(fpath)
                        needs_save = True
                for take in shot.get("takes", []):
                    if take.get("file") and not take.get("media_info"):
                        fpath = os.path.join(shot_dir, os.path.basename(take["file"]))
                        if os.path.exists(fpath):
                            take["media_info"] = get_media_metadata(fpath)
                            needs_save = True
            if needs_save:
                save_project(data)
            return data
        except Exception as e:
            print(f"[HATA] Proje okunamadı: {e}")
    return None

def save_project(project_data):
    project_id = clean_id(project_data.get("id"))
    if not project_id:
        return False
    json_path = get_project_json_path(project_id)
    bak_path = json_path + ".bak"
    tmp_path = json_path + f".tmp_{int(time.time() * 1000)}"
    with project_file_lock:
        try:
            # 1. Önce geçici dosyaya yaz
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(project_data, f, ensure_ascii=False, indent=2)
            # 2. Eğer mevcut dosya varsa yedeğini al
            if os.path.exists(json_path):
                try:
                    shutil.copy2(json_path, bak_path)
                except Exception:
                    pass
            # 3. Geçici dosyayı atomik olarak hedef dosyanın üzerine taşı
            os.replace(tmp_path, json_path)
            with state_lock:
                engine_state["project_version"] = int(time.time() * 1000)
            return True
        except Exception as e:
            print(f"[HATA] Proje kaydedilemedi: {e}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
            return False

def list_all_projects():
    projects = []
    if not os.path.exists(PROJECTS_DIR):
        os.makedirs(PROJECTS_DIR, exist_ok=True)
        return projects

    for item in sorted(os.listdir(PROJECTS_DIR)):
        item_path = os.path.join(PROJECTS_DIR, item)
        if os.path.isdir(item_path):
            pdata = load_project(item)
            if pdata:
                projects.append({
                    "id": pdata.get("id", item),
                    "name": pdata.get("name", item),
                    "created_at": pdata.get("created_at", ""),
                    "shot_count": len(pdata.get("shots", []))
                })
            else:
                projects.append({
                    "id": item,
                    "name": item.replace("_", " ").title(),
                    "created_at": "",
                    "shot_count": 0
                })
    return projects

def create_new_project(name):
    if not name or not name.strip():
        name = "Yeni Proje"
    name = name.strip()
    
    existing = [p["id"] for p in list_all_projects()]
    index = len(existing) + 1
    safe_slug = slugify(name)
    project_id = f"proje_{index}_{safe_slug}" if safe_slug else f"proje_{index}"
    
    project_dir = get_project_path(project_id)
    os.makedirs(project_dir, exist_ok=True)
    
    # Varsayılan Sahne Klasörleri
    shot1_dir = os.path.join(project_dir, "shot_01")
    shot2_dir = os.path.join(project_dir, "shot_02")
    os.makedirs(shot1_dir, exist_ok=True)
    os.makedirs(shot2_dir, exist_ok=True)

    project_data = {
        "id": project_id,
        "name": name,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "shots": [
            {
                "id": "shot_01",
                "title": "Shot 01: Giriş / Açılış",
                "duration": 4,
                "skill": "brainlab-cinematic-output",
                "prompt": "Wide cinematic establishing shot, golden hour lighting, 35mm lens.",
                "camera": "Slow Pan Right (15°)",
                "speed": "1.0x",
                "angle": "Eye-level",
                "file": None,
                "file_type": None
            },
            {
                "id": "shot_02",
                "title": "Shot 02: Karakter Odak",
                "duration": 5,
                "skill": "brainlab-cinematic-output",
                "prompt": "Medium close-up, protagonist walking with intense emotion, cinematic rim lighting, 50mm lens, film grain.",
                "camera": "Push-in Dolly",
                "speed": "1.2x",
                "angle": "Low-angle 15°",
                "file": None,
                "file_type": None
            }
        ]
    }
    save_project(project_data)
    return project_data

def is_ai_media_file(filename):
    """
    Yalnızca Higgsfield veya AI video/görsel araçlarından inen dosyaları tespit eder.
    Kullanıcının Photoshop'tan masaüstüne kaydettiği veya internetten indirdiği
    rastgele dosyaların sahneye otomatik yüklenmesini kesin olarak engeller.
    """
    lower = filename.lower()
    # macOS Ekran Görüntüleri ve geçici/taslak dosyaları kesinlikle yoksay
    if lower.startswith(('ekran resmi', 'screen shot', 'screenshot', 'untitled', 'adsız', '~', '.')):
        return False
    # Higgsfield varsayılan dosya adı şablonu (Örn: hf_20261113_... veya higgsfield_...)
    if lower.startswith('hf_') or 'higgsfield' in lower:
        return True
    # Diğer popüler yapay zeka araçları
    if any(k in lower for k in ('kling', 'seedance', 'veo', 'runway', 'luma', 'midjourney', 'suno')):
        return True
    return False

def get_chrome_higgsfield_model():
    script = '''
    tell application "Google Chrome"
        repeat with w in windows
            repeat with t in tabs of w
                set u to URL of t
                if u contains "higgsfield.ai" then
                    return u & "|||" & (title of t)
                end if
            end repeat
        end repeat
    end tell
    return ""
    '''
    try:
        res = subprocess.run(['osascript', '-e', script], capture_output=True, text=True, timeout=1)
        val = res.stdout.strip()
        if val and '|||' in val:
            url, title = val.split('|||', 1)
            parsed_u = urlparse(url)
            qs = parse_qs(parsed_u.query)
            if 'model' in qs and qs['model']:
                raw_m = qs['model'][0]
                clean_m = raw_m.replace('-', ' ').replace('_', ' ')
                if 'gpt' in clean_m.lower() or 'sunburst' in clean_m.lower():
                    return clean_m.title().replace('Gpt', 'GPT')
                elif 'kling' in clean_m.lower():
                    return 'Kling AI'
                elif 'seedance' in clean_m.lower():
                    return 'Seedance'
                elif 'wan' in clean_m.lower():
                    return 'Wan 2.1'
                elif 'veo' in clean_m.lower():
                    return 'Veo 2'
                elif 'soul' in clean_m.lower():
                    return 'Higgsfield Soul'
                elif 'nano' in clean_m.lower():
                    return 'Higgsfield Nano'
                return clean_m.title()
            if '|' in title:
                t_part = title.split('|')[0].strip()
                if t_part and t_part.lower() != 'higgsfield':
                    return t_part
    except Exception:
        pass
    return None

# Watcher (İndirilenler & Masaüstü Klasörü İzleyici)
def downloads_watcher_loop():
    print(f"[WATCHER] İzleme başladı: {WATCH_DIRS} (Yalnızca Higgsfield ve AI çıktıları filtreleniyor)")
    valid_exts = ('.mp4', '.mov', '.webm', '.png', '.jpg', '.jpeg', '.webp')
    
    # Başlangıçta var olan dosyaları hafızaya al
    seen_files = set()
    for wdir in WATCH_DIRS:
        try:
            if os.path.exists(wdir):
                for f in os.listdir(wdir):
                    fpath = os.path.join(wdir, f)
                    if not fpath.startswith(STUDIO_DIR):
                        seen_files.add(fpath)
        except Exception as e:
            print(f"[WATCHER HATA] {wdir} taranamadı: {e}")

    file_size_tracker = {}

    while True:
        try:
            time.sleep(1.0)
            for wdir in WATCH_DIRS:
                if not os.path.exists(wdir):
                    continue
                try:
                    entries = os.listdir(wdir)
                except OSError:
                    continue

                for filename in entries:
                    file_path = os.path.join(wdir, filename)
                    if file_path.startswith(STUDIO_DIR) or os.path.isdir(file_path):
                        continue
                    if not filename.lower().endswith(valid_exts) or filename.startswith('.'):
                        continue
                    if file_path in seen_files:
                        continue

                    # YALNIZCA Higgsfield ve AI araçlarının çıktılarını otomatik yakala:
                    # Kullanıcının Photoshop masaüstü kaydetmeleri veya internet indirmeleri yakalanmaz!
                    if not is_ai_media_file(filename):
                        seen_files.add(file_path)
                        continue

                    try:
                        current_size = os.path.getsize(file_path)
                    except OSError:
                        continue

                    # Dosyanın indirilmesinin tamamlandığını kontrol et (boyut 1 saniye sabit kalmalı)
                    last_size = file_size_tracker.get(file_path)
                    if last_size is None or last_size != current_size:
                        file_size_tracker[file_path] = current_size
                        continue  # Hala iniyor
                    
                    # Dosya tamamen inmiş!
                    seen_files.add(file_path)
                    if file_path in file_size_tracker:
                        del file_size_tracker[file_path]

                    with state_lock:
                        active_proj = engine_state.get("active_project_id")
                        active_shot = engine_state.get("active_shot_id")

                    if not active_proj or not active_shot:
                        print(f"[WATCHER] Yeni dosya bulundu ({filename}) ama aktif sahne seçili değil.")
                        continue

                    pdata = load_project(active_proj)
                    if not pdata:
                        continue

                    # Dosyayı sahne klasörüne taşı
                    target_dir = os.path.join(get_project_path(active_proj), active_shot)
                    os.makedirs(target_dir, exist_ok=True)
                    target_path = os.path.join(target_dir, filename)

                    try:
                        shutil.copy2(file_path, target_path)
                        print(f"🔥 [YAKALANDI] {filename} ({wdir}) -> {active_proj} / {active_shot} içine aktarıldı!")

                        # Projeyi güncelle (Takes & Reçete Künyesi)
                        take_id = "take_01"
                        for shot in pdata.get("shots", []):
                            if shot.get("id") == active_shot:
                                file_type = "video" if filename.lower().endswith(('.mp4', '.mov', '.webm')) else "image"
                                now_str = time.strftime("%Y-%m-%d %H:%M:%S")

                                if "takes" not in shot or not isinstance(shot["takes"], list):
                                    shot["takes"] = []

                                take_id = get_next_take_id(shot["takes"])

                                # Bu görseli/videoyu üreten aktif reçetenin EXIF anlık görüntüsü
                                meta = get_media_metadata(target_path)
                                tab_model = get_chrome_higgsfield_model()
                                if tab_model and meta:
                                    meta["model"] = tab_model
                                    meta["model_source"] = tab_model
                                    print(f"🎯 [CHROME SEKME TESPİTİ] Model yakalandı: {tab_model}")

                                new_take = {
                                    "id": take_id,
                                    "file": filename,
                                    "file_type": file_type,
                                    "captured_at": now_str,
                                    "prompt": shot.get("prompt", ""),
                                    "skills": list(shot.get("skills", [shot.get("skill", "brainlab-cinematic-output")])),
                                    "references": list(shot.get("references", [])),
                                    "styles": list(shot.get("styles", [])),
                                    "camera": shot.get("camera", ""),
                                    "is_master": True,
                                    "media_info": meta
                                }

                                # Önceki take'lerin master durumunu kaldırıp yenisini master yap
                                for t in shot["takes"]:
                                    t["is_master"] = False

                                shot["takes"].append(new_take)
                                shot["file"] = filename
                                shot["file_type"] = file_type
                                shot["media_info"] = new_take["media_info"]
                                shot["active_take_id"] = take_id
                                shot["captured_at"] = now_str
                                break
                        save_project(pdata)

                        with state_lock:
                            engine_state["last_captured"] = {
                                "filename": filename,
                                "project_id": active_proj,
                                "shot_id": active_shot,
                                "take_id": take_id,
                                "timestamp": time.strftime("%H:%M:%S")
                            }
                    except Exception as ex:
                        print(f"[WATCHER HATA] Dosya aktarılamadı: {ex}")

        except Exception as e:
            time.sleep(2.0)

# HTTP Request Handler
class StudioServerHandler(BaseHTTPRequestHandler):
    timeout = 15

    def end_headers(self):
        origin = self.headers.get("Origin", "")
        if origin in ("http://localhost:8765", "http://127.0.0.1:8765"):
            self.send_header("Access-Control-Allow-Origin", origin)
        else:
            self.send_header("Access-Control-Allow-Origin", "http://localhost:8765")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/" or path == "/dashboard":
            dashboard_file = os.path.join(APP_DIR, "dashboard.html")
            if os.path.exists(dashboard_file):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(dashboard_file, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "dashboard.html bulunamadi")

        elif path == "/tailwindcss.min.js":
            tw_file = os.path.join(APP_DIR, "tailwindcss.min.js")
            if os.path.exists(tw_file):
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Cache-Control", "public, max-age=31536000")
                self.end_headers()
                with open(tw_file, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "tailwindcss.min.js bulunamadi")

        elif path == "/api/projects":
            projects = list_all_projects()
            self.send_json({"projects": projects, "active_project_id": engine_state["active_project_id"]})

        elif path == "/api/project":
            proj_id = params.get("id", [None])[0] or engine_state.get("active_project_id")
            if not proj_id:
                projects = list_all_projects()
                if projects:
                    proj_id = projects[0]["id"]
            
            pdata = load_project(proj_id) if proj_id else None
            if pdata:
                with state_lock:
                    engine_state["active_project_id"] = proj_id
                    if not engine_state.get("active_shot_id") and pdata.get("shots"):
                        engine_state["active_shot_id"] = pdata["shots"][0]["id"]
                self.send_json(pdata)
            else:
                self.send_json({"error": "Proje bulunamadi"}, 404)

        elif path == "/api/status":
            with state_lock:
                self.send_json(engine_state)

        elif path == "/api/media":
            # Medya dosyasını güvenli sun (Yalnızca proje içi)
            proj_id = clean_id(params.get("project", [None])[0])
            shot_id = clean_id(params.get("shot", [None])[0])
            filename = os.path.basename(params.get("file", [""])[0])

            if not (proj_id and shot_id and filename):
                self.send_error(400, "Eksik parametre")
                return

            file_path = os.path.join(get_project_path(proj_id), shot_id, filename)
            if not is_safe_project_path(file_path):
                self.send_error(403, "Guvensiz dosya yolu")
                return

            if os.path.exists(file_path):
                mime, _ = mimetypes.guess_type(file_path)
                mime = mime or "application/octet-stream"
                file_size = os.path.getsize(file_path)

                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(file_size))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()
                with open(file_path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
            else:
                self.send_error(404, "Medya dosyasi bulunamadi")

        elif path == "/api/asset":
            # Referans ve Stil thumbnail görsellerini sun (Yalnızca proje içi)
            proj_id = clean_id(params.get("project", [None])[0])
            shot_id = clean_id(params.get("shot", [None])[0])
            category = clean_id(params.get("category", [None])[0])
            filename = os.path.basename(params.get("file", [""])[0])

            if not (proj_id and shot_id and category in ("references", "styles") and filename):
                self.send_error(400, "Eksik veya gecersiz parametre")
                return

            file_path = os.path.join(get_project_path(proj_id), shot_id, category, filename)
            if not is_safe_project_path(file_path):
                self.send_error(403, "Guvensiz dosya yolu")
                return

            if os.path.exists(file_path):
                mime, _ = mimetypes.guess_type(file_path)
                mime = mime or "image/jpeg"
                file_size = os.path.getsize(file_path)

                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.send_header("Content-Length", str(file_size))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                with open(file_path, "rb") as f:
                    shutil.copyfileobj(f, self.wfile)
            else:
                self.send_error(404, "Asset dosyasi bulunamadi")

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self.read_body_json()

        if path == "/api/projects/create":
            name = body.get("name", "Yeni Proje")
            new_proj = create_new_project(name)
            with state_lock:
                engine_state["active_project_id"] = new_proj["id"]
                if new_proj.get("shots"):
                    engine_state["active_shot_id"] = new_proj["shots"][0]["id"]
            self.send_json({"success": True, "project": new_proj})

        elif path == "/api/projects/rename":
            proj_id = body.get("id")
            new_name = body.get("name", "").strip()
            if not (proj_id and new_name):
                self.send_json({"error": "Gecersiz isim veya ID"}, 400)
                return
            pdata = load_project(proj_id)
            if pdata:
                pdata["name"] = new_name
                save_project(pdata)
                self.send_json({"success": True, "project": pdata})
            else:
                self.send_json({"error": "Proje bulunamadi"}, 404)

        elif path == "/api/projects/delete":
            proj_id = body.get("id")
            if not proj_id:
                self.send_json({"error": "Gecersiz ID"}, 400)
                return
            proj_dir = get_project_path(proj_id)
            if os.path.exists(proj_dir):
                send_to_trash(proj_dir)

            projects = list_all_projects()
            if not projects:
                default_p = create_new_project("Yeni Proje")
                projects = [default_p]

            new_active = projects[0]["id"]
            with state_lock:
                engine_state["active_project_id"] = new_active
                p = load_project(new_active)
                if p and p.get("shots"):
                    engine_state["active_shot_id"] = p["shots"][0]["id"]
                else:
                    engine_state["active_shot_id"] = None

            self.send_json({"success": True, "active_project_id": new_active, "projects": projects})

        elif path == "/api/open_folder":
            # macOS Finder'da doğrudan klasörü aç (Yalnızca proje klasörü içiyle sınırlı)
            proj_id = clean_id(body.get("project_id") or engine_state.get("active_project_id"))
            shot_id = clean_id(body.get("shot_id") or engine_state.get("active_shot_id"))

            target_dir = None
            if proj_id:
                if shot_id:
                    shot_path = os.path.join(get_project_path(proj_id), shot_id)
                    if os.path.exists(shot_path) and is_safe_project_path(shot_path):
                        target_dir = shot_path
                if not target_dir:
                    proj_path = get_project_path(proj_id)
                    if os.path.exists(proj_path) and is_safe_project_path(proj_path):
                        target_dir = proj_path

            if not target_dir or not is_safe_project_path(target_dir):
                target_dir = PROJECTS_DIR

            try:
                subprocess.run(["open", target_dir], check=True)
                print(f"📂 [FINDER AÇILDI] {target_dir}")
                self.send_json({"success": True, "path": target_dir})
            except Exception as ex:
                print(f"[HATA] Finder açılamadı: {ex}")
                self.send_json({"error": str(ex)}, 500)

        elif path == "/api/set_active_shot":
            proj_id = body.get("project_id")
            shot_id = body.get("shot_id")
            with state_lock:
                if proj_id:
                    engine_state["active_project_id"] = proj_id
                if shot_id:
                    engine_state["active_shot_id"] = shot_id
            print(f"[HEDEF GÜNCELLENDİ] Proje: {proj_id} | Sahne: {shot_id}")
            self.send_json({"success": True, "state": engine_state})

        elif path == "/api/project/update_shot":
            proj_id = body.get("project_id")
            shot_data = body.get("shot")
            if not (proj_id and shot_data):
                self.send_json({"error": "Eksik veri"}, 400)
                return
            pdata = load_project(proj_id)
            if pdata:
                found = False
                for i, s in enumerate(pdata.get("shots", [])):
                    if s["id"] == shot_data["id"]:
                        pdata["shots"][i].update(shot_data)
                        found = True
                        break
                if found:
                    save_project(pdata)
                    self.send_json({"success": True, "project": pdata})
                else:
                    self.send_json({"error": "Sahne bulunamadi"}, 404)
            else:
                self.send_json({"error": "Proje bulunamadi"}, 404)

        elif path == "/api/project/set_active_take":
            proj_id = body.get("project_id")
            shot_id = body.get("shot_id")
            take_id = body.get("take_id")
            make_master = body.get("is_master", False)
            pdata = load_project(proj_id)
            if pdata:
                found = False
                for shot in pdata.get("shots", []):
                    if shot["id"] == shot_id:
                        for t in shot.get("takes", []):
                            if t["id"] == take_id:
                                shot["active_take_id"] = take_id
                                shot["file"] = t["file"]
                                shot["file_type"] = t["file_type"]
                                shot["media_info"] = t.get("media_info")
                                shot["prompt"] = t.get("prompt", shot.get("prompt", ""))
                                shot["skills"] = list(t.get("skills", shot.get("skills", [])))
                                shot["references"] = list(t.get("references", []))
                                shot["styles"] = list(t.get("styles", []))
                                if t.get("camera"):
                                    shot["camera"] = t["camera"]
                                if make_master:
                                    for ot in shot.get("takes", []):
                                        ot["is_master"] = (ot["id"] == take_id)
                                found = True
                                break
                        break
                if found:
                    save_project(pdata)
                    self.send_json({"success": True, "project": pdata})
                else:
                    self.send_json({"error": "Take veya Sahne bulunamadi"}, 404)
            else:
                self.send_json({"error": "Proje bulunamadi"}, 404)

        elif path == "/api/project/delete_take":
            proj_id = body.get("project_id")
            shot_id = body.get("shot_id")
            take_id = body.get("take_id")
            pdata = load_project(proj_id)
            if pdata:
                found = False
                for shot in pdata.get("shots", []):
                    if shot["id"] == shot_id:
                        takes = shot.get("takes", [])
                        target_take = next((t for t in takes if t["id"] == take_id), None)
                        if target_take:
                            fpath = os.path.join(get_project_path(proj_id), shot_id, target_take["file"])
                            send_to_trash(fpath)
                            shot["takes"] = [t for t in takes if t["id"] != take_id]
                            if shot.get("active_take_id") == take_id:
                                if shot["takes"]:
                                    rem = shot["takes"][-1]
                                    shot["active_take_id"] = rem["id"]
                                    shot["file"] = rem["file"]
                                    shot["file_type"] = rem["file_type"]
                                    shot["prompt"] = rem.get("prompt", "")
                                    shot["skills"] = list(rem.get("skills", []))
                                    shot["references"] = list(rem.get("references", []))
                                    shot["styles"] = list(rem.get("styles", []))
                                else:
                                    shot["active_take_id"] = None
                                    shot["file"] = None
                                    shot["file_type"] = None
                                    shot["references"] = []
                                    shot["styles"] = []
                            found = True
                        break
                if found:
                    save_project(pdata)
                    self.send_json({"success": True, "project": pdata})
                else:
                    self.send_json({"error": "Take bulunamadi"}, 404)
            else:
                self.send_json({"error": "Proje bulunamadi"}, 404)

        elif path == "/api/take/update_model":
            proj_id = body.get("project_id") or engine_state.get("active_project_id")
            shot_id = body.get("shot_id")
            take_id = body.get("take_id")
            new_model = body.get("model", "").strip()
            if not (proj_id and shot_id and new_model):
                self.send_json({"error": "Eksik parametre"}, 400)
                return
            pdata = load_project(proj_id)
            if pdata:
                for shot in pdata.get("shots", []):
                    if shot.get("id") == shot_id:
                        if not take_id or shot.get("active_take_id") == take_id:
                            if "media_info" not in shot or not isinstance(shot["media_info"], dict):
                                shot["media_info"] = {}
                            shot["media_info"]["model"] = new_model
                        for take in shot.get("takes", []):
                            if not take_id or take.get("id") == take_id:
                                if "media_info" not in take or not isinstance(take["media_info"], dict):
                                    take["media_info"] = {}
                                take["media_info"]["model"] = new_model
                        break
                save_project(pdata)
                self.send_json({"success": True, "project": pdata})
            else:
                self.send_json({"error": "Proje bulunamadi"}, 404)

        elif path == "/api/take/update_credits":
            proj_id = body.get("project_id") or engine_state.get("active_project_id")
            shot_id = body.get("shot_id")
            take_id = body.get("take_id")
            try:
                new_credits = int(body.get("credits", 0))
            except Exception:
                new_credits = 0

            if not (proj_id and shot_id):
                self.send_json({"error": "Eksik parametre"}, 400)
                return
            pdata = load_project(proj_id)
            if pdata:
                for shot in pdata.get("shots", []):
                    if shot.get("id") == shot_id:
                        if not take_id or shot.get("active_take_id") == take_id:
                            if "media_info" not in shot or not isinstance(shot["media_info"], dict):
                                shot["media_info"] = {}
                            shot["media_info"]["credits"] = new_credits
                        for take in shot.get("takes", []):
                            if not take_id or take.get("id") == take_id:
                                if "media_info" not in take or not isinstance(take["media_info"], dict):
                                    take["media_info"] = {}
                                take["media_info"]["credits"] = new_credits
                        break
                save_project(pdata)
                self.send_json({"success": True, "project": pdata})
            else:
                self.send_json({"error": "Proje bulunamadi"}, 404)

        elif path == "/api/project/add_asset":
            # Referans veya Stil görseli yükle (base64)
            import base64
            proj_id = body.get("project_id")
            shot_id = body.get("shot_id")
            category = body.get("category")  # "references" veya "styles"
            filename = body.get("filename", f"asset_{int(time.time())}.jpg")
            data_b64 = body.get("data")

            if not (proj_id and shot_id and category in ("references", "styles") and data_b64):
                self.send_json({"error": "Eksik veri"}, 400)
                return

            pdata = load_project(proj_id)
            if not pdata:
                self.send_json({"error": "Proje bulunamadi"}, 404)
                return

            # Klasörü hazırla
            target_dir = os.path.join(get_project_path(proj_id), shot_id, category)
            os.makedirs(target_dir, exist_ok=True)
            safe_filename = slugify(os.path.splitext(filename)[0]) + os.path.splitext(filename)[1]
            target_path = os.path.join(target_dir, safe_filename)

            try:
                # data:image/png;base64,.... başlığını temizle
                if "," in data_b64:
                    data_b64 = data_b64.split(",", 1)[1]
                file_bytes = base64.b64decode(data_b64)
                with open(target_path, "wb") as f:
                    f.write(file_bytes)

                # Shot verisini güncelle
                target_take_id = body.get("take_id")
                for shot in pdata.get("shots", []):
                    if shot["id"] == shot_id:
                        if category not in shot or not isinstance(shot[category], list):
                            shot[category] = []
                        if safe_filename not in shot[category]:
                            shot[category].append(safe_filename)

                        # Aktif take verisini de güncelle
                        active_t = target_take_id or shot.get("active_take_id")
                        if active_t:
                            for t in shot.get("takes", []):
                                if t["id"] == active_t:
                                    if category not in t or not isinstance(t[category], list):
                                        t[category] = []
                                    if safe_filename not in t[category]:
                                        t[category].append(safe_filename)
                        break
                save_project(pdata)
                self.send_json({"success": True, "filename": safe_filename, "project": pdata})
            except Exception as ex:
                print(f"[ASSET HATA] Görsel kaydedilemedi: {ex}")
                self.send_json({"error": str(ex)}, 500)

        elif path == "/api/project/remove_asset":
            proj_id = body.get("project_id")
            shot_id = body.get("shot_id")
            take_id = body.get("take_id")
            category = body.get("category")
            filename = body.get("filename")

            pdata = load_project(proj_id)
            if not pdata:
                self.send_json({"error": "Proje bulunamadi"}, 404)
                return

            for shot in pdata.get("shots", []):
                if shot["id"] == shot_id:
                    active_t = take_id or shot.get("active_take_id")
                    if active_t:
                        for t in shot.get("takes", []):
                            if t["id"] == active_t and category in t:
                                t[category] = [f for f in t[category] if f != filename]
                    if category in shot:
                        shot[category] = [f for f in shot[category] if f != filename]
                    break

            is_used_elsewhere = False
            for s in pdata.get("shots", []):
                if s["id"] == shot_id:
                    if filename in s.get(category, []):
                        is_used_elsewhere = True
                    for t in s.get("takes", []):
                        if filename in t.get(category, []):
                            is_used_elsewhere = True

            if not is_used_elsewhere:
                file_path = os.path.join(get_project_path(proj_id), shot_id, category, filename)
                if is_safe_project_path(file_path):
                    send_to_trash(file_path)

            save_project(pdata)
            self.send_json({"success": True, "project": pdata})

        elif path == "/api/project/add_shot":
            proj_id = clean_id(body.get("project_id"))
            title = body.get("title")
            pdata = load_project(proj_id)
            if pdata:
                shots = pdata.get("shots", [])
                shot_id = get_next_shot_id(shots)
                next_num = int(shot_id.split("_")[1]) if "_" in shot_id else len(shots) + 1
                shot_dir = os.path.join(get_project_path(proj_id), shot_id)
                os.makedirs(shot_dir, exist_ok=True)

                new_shot = {
                    "id": shot_id,
                    "title": title or f"Shot {next_num:02d}: Yeni Sahne",
                    "duration": 4,
                    "skill": "brainlab-cinematic-output",
                    "prompt": "Cinematic shot, dramatic lighting, 35mm anamorphic lens.",
                    "camera": "Push-in Dolly",
                    "speed": "1.0x",
                    "angle": "Eye-level",
                    "file": None,
                    "file_type": None
                }
                shots.append(new_shot)
                pdata["shots"] = shots
                save_project(pdata)
                with state_lock:
                    engine_state["active_shot_id"] = shot_id
                    engine_state["project_version"] = int(time.time() * 1000)
                self.send_json({"success": True, "new_shot": new_shot, "project": pdata})
            else:
                self.send_json({"error": "Proje bulunamadi"}, 404)

        elif path == "/api/project/delete_shot":
            proj_id = body.get("project_id")
            shot_id = body.get("shot_id")
            pdata = load_project(proj_id)
            if pdata:
                shots = pdata.get("shots", [])
                if len(shots) <= 1:
                    self.send_json({"error": "Projede en az 1 sahne bulunmalidir"}, 400)
                    return

                new_shots = [s for s in shots if s["id"] != shot_id]
                pdata["shots"] = new_shots
                save_project(pdata)

                # Fiziksel klasörü Mac Çöp Sepetine gönder
                shot_dir = os.path.join(get_project_path(proj_id), shot_id)
                send_to_trash(shot_dir)
                
                with state_lock:
                    if engine_state.get("active_shot_id") == shot_id:
                        engine_state["active_shot_id"] = new_shots[0]["id"]

                self.send_json({"success": True, "project": pdata, "new_active_shot": new_shots[0]["id"]})
            else:
                self.send_json({"error": "Proje bulunamadi"}, 404)
        else:
            self.send_error(404)

    def read_body_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if 0 < length <= 25 * 1024 * 1024:
                body_bytes = self.rfile.read(length)
                return json.loads(body_bytes.decode("utf-8"))
        except Exception:
            pass
        return {}

    def send_json(self, data, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

def main():
    # İlk varsayılan projeyi oluştur (eğer hiç proje yoksa)
    projects = list_all_projects()
    if not projects:
        default_proj = create_new_project("Cyberpunk Kısa Film")
        engine_state["active_project_id"] = default_proj["id"]
        engine_state["active_shot_id"] = "shot_01"
    else:
        engine_state["active_project_id"] = projects[0]["id"]
        p = load_project(projects[0]["id"])
        if p and p.get("shots"):
            engine_state["active_shot_id"] = p["shots"][0]["id"]

    # Watcher thread başlat
    watcher_thread = threading.Thread(target=downloads_watcher_loop, daemon=True)
    watcher_thread.start()

    # Web sunucu başlat (Yalnızca yerel bilgisayara açık: 127.0.0.1 - Güvenli)
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("127.0.0.1", PORT), StudioServerHandler)
    print(f"🎬 [INFOSHOT ENGINE] Çalışıyor: http://localhost:{PORT}")
    print(f"📁 Projeler Dizini: {PROJECTS_DIR}")
    print(f"👀 İzlenen Klasör: {DOWNLOADS_DIR}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFOSHOT ENGINE] Kapatılıyor...")
        server.server_close()

if __name__ == "__main__":
    main()
