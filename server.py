#!/usr/bin/env python3
"""Imagine: private web UI for Qwen-Image 2.1 on Predator.

Listens on 127.0.0.1:8790; published tailnet-only with
`tailscale serve --bg --https=8790 http://127.0.0.1:8790`.

Modes
  normal     image saved to the gallery (IMAGINE_GALLERY, default ~/.hermes/image_cache/qwen-image).
  temporary  image kept in RAM, served once, then dropped.

Variations: a request can ask for N images, or "keep going" until stopped;
each run uses a fresh random seed. The series keeps its prompt in RAM only
while it is active.

Stories: /api/story/* writes a story with the same local LLM (or saves one you
wrote), and turns a story into a picture story: the LLM splits it into scenes
with fixed character descriptions, then each scene is queued as an image.
Stories live in STORIES as JSON.
A story can have a cast: named photos of characters/objects. The LLM sees the
photos (vision) when writing and planning; each scene is drawn with the photos
of whoever is in it as reference images. Cast photos live in STORIES/<id>/.

Library: reusable named reference photos, stored in ASSETS.

Prompt improver: /api/improve rewrites a prompt with a local LLM
(llama-server on 127.0.0.1:8084, systemd --user prompt-llm). Nothing is
stored or logged; the text only passes through RAM here and there.

Hardening: the process is non-dumpable (no core dumps), prompts never go on a
command line or into a log, and temporary output returns over ComfyUI's
websocket so it never touches the render machine's disk.
"""
import base64, ctypes, importlib.machinery, importlib.util, json, os, queue, random, resource
import re, threading, time, urllib.request, uuid
from argparse import Namespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, unquote


PORT = int(os.environ.get("IMAGINE_PORT", 8790))
HOME = Path.home()
IMG_DIR = Path(os.environ.get("IMAGINE_GALLERY", HOME / ".hermes/image_cache/qwen-image"))
DATA = HOME / ".local/share/imagine"
STORIES = DATA / "stories"       # normal-mode stories (plaintext JSON)
ASSETS = DATA / "assets"         # normal-mode library: index.json + image files
PAGE = Path(__file__).with_name("index.html")
ASPECTS = {"square", "portrait", "landscape", "wide", "tall"}
TEMP_TTL = 30 * 60
MAX_COUNT = 20
LLM = os.environ.get("IMAGINE_LLM", "http://127.0.0.1:8084/v1/chat/completions")
IMPROVE_SYSTEM = """You rewrite prompts for the Qwen-Image text-to-image model.
Keep exactly what the user asked for: the same subject, content and every explicit or adult detail. Do not censor, soften, moralize, add warnings or change the idea.
Write one vivid, concrete English paragraph of 40-90 words covering subject and appearance, action or pose, setting, composition and camera angle, lighting, and style or medium.
Any text that should appear inside the image stays word-for-word in double quotes.
If it is an edit instruction for existing images (it mentions <image1>, <image2>, or you are told it is an edit), keep it an edit instruction: keep the <imageN> references, say precisely what to change and what must stay the same.
Never sexualize minors. If the request does, output only: REFUSED
Output only the rewritten prompt, nothing else."""

_loader = importlib.machinery.SourceFileLoader("qwen_image", os.environ.get("QWEN_IMAGE", str(Path(__file__).with_name("qwen-image"))))
_spec = importlib.util.spec_from_loader("qwen_image", _loader)
qi = importlib.util.module_from_spec(_spec)
_loader.exec_module(qi)

jobs = {}          # id -> public job record
series = {}        # id -> {"private": {...}, "params": {...}, "left": int|None, "done": int}
temp_data = {}     # id -> PNG bytes for temporary jobs
order = []         # newest first
work = queue.Queue()
lock = threading.Lock()
improve_lock = threading.Lock()  # one LLM request at a time
story_work = queue.Queue()
story_private = {}  # job id -> {"idea", "genre", "words", "text", ...}; RAM only
story_lock = threading.Lock()
LENGTHS = {"short": 300, "medium": 700, "long": 1200}
STORY_SYSTEM = """You are a skilled fiction writer. Write the story the user asks for, complete, with a real beginning, middle and end, vivid scenes and natural dialogue.
Keep every element the user asks for, including explicit or adult content, without censoring, softening, moralizing or adding warnings.
Never sexualize minors. If the request does, output only: REFUSED
Format: the first line is "TITLE: <a short title>", then a blank line, then the story in plain paragraphs. No notes before or after."""
REVISE_SYSTEM = """You are a fiction editor. You get a story and an instruction from its author.
Keep every element of the story that the instruction doesn't change, including explicit or adult content, without censoring, softening, moralizing or adding warnings.
Never sexualize minors. If asked to, output only: REFUSED
{how}
Format: the first line is "TITLE: <title>", then a blank line, then the story in plain paragraphs. No notes before or after."""
REVISE_HOW = {
    "rewrite": "Apply the instruction and return the complete revised story from start to finish (not a summary, not only the changed parts).",
    "continue": "Write only the continuation that follows the story's last line, following the instruction, in the same voice. Repeat the existing title.",
}
BOARD_SYSTEM = """You turn a story into an illustrated picture story for the Qwen-Image text-to-image model.
First fix how every main character looks (age range, build, face, hair, skin, clothing) in one line each. Then split the story into exactly {n} key scenes in order.
For each scene write a caption (1-2 sentences that narrate that moment) and an image_prompt of 50-90 words: repeat the full fixed look of every character in the scene word-for-word so they stay identical across images, then the action or pose, setting, composition and camera angle, lighting, and the style: {style}.
Keep the story's content as it is, including explicit or adult details. Never sexualize minors; if the story does, return an empty scenes list.
Return JSON only."""
BOARD_SCHEMA = {"type": "object", "properties": {
    "characters": {"type": "string"},
    "scenes": {"type": "array", "items": {"type": "object", "properties": {
        "caption": {"type": "string"}, "image_prompt": {"type": "string"}},
        "required": ["caption", "image_prompt"]}}},
    "required": ["characters", "scenes"]}


def b64e(b): return base64.b64encode(b).decode()
def b64d(s): return base64.b64decode(s)


# ── jobs and series ─────────────────────────────────────────────────────────

def enqueue(sid, seed=None):
    """Queue the next image of series sid. Caller holds no lock."""
    s = series[sid]
    p, priv = s["params"], s["private"]
    jid = uuid.uuid4().hex[:12]
    seed = seed if seed is not None else random.randint(0, 2**31)
    job = {"id": jid, "series": sid, "mode": p["mode"], "aspect": p["aspect"], "fast": p["fast"],
           "edit": len(priv["images"]), "seed": seed, "status": "queued", "created": time.time(),
           "n": s["queued"] + 1, "of": s["left_total"]}
    if p.get("story"):
        job.update(story=p["story"], scene=p["scene"])
    job["prompt"] = priv["prompt"]
    s["queued"] += 1
    with lock:
        jobs[jid] = job
        order.insert(0, jid)
    work.put(jid)
    return job


def end_series(sid):
    s = series.pop(sid, None)
    if s:
        s["private"].clear()


def worker():
    while True:
        jid = work.get()
        job = jobs.get(jid)
        s = series.get(job["series"]) if job else None
        if not job or not s:
            if job:  # series was stopped while this was queued
                job.update(status="stopped", finished=time.time())
            continue
        job.update(status="running", started=time.time())
        priv = s["private"]
        a = Namespace(prompt=priv["prompt"], negative=priv["negative"], size=None, aspect=job["aspect"],
                      steps=25, seed=job["seed"], fast=job["fast"], temp=job["mode"] != "normal",
                      cast=bool(s["params"].get("cast")))
        try:
            png = qi.render(a, priv["images"], log=lambda _: None)
            if job["mode"] == "temporary":
                temp_data[jid] = png
            else:
                name = f"qwen_{time.strftime('%Y%m%d_%H%M%S')}_{a.seed}.png"
                tmp = IMG_DIR / (name + ".part")  # the gallery lists *.png only, so it
                tmp.write_bytes(png)              # never sees a half-written file
                tmp.replace(IMG_DIR / name)
                job["image"] = name
                if job.get("story"):
                    story_update(job["story"], lambda st: scene_set_image(st["scenes"][job["scene"]], name))
            job["status"] = "done"
            if priv.get("chain") and sid_active(job["series"]):
                nxt, rest = priv["chain"][0], priv["chain"][1:]
                p = s["params"]
                scene_series(nxt["prompt"], nxt["cast"], {k: p[k] for k in ("mode", "fast", "aspect", "story")}
                             | {"scene": nxt["scene"], "caption": nxt["caption"]}, nxt["base"], png, rest)
        except BaseException as e:  # render() may sys.exit when Predator is down
            msg = str(e) or type(e).__name__
            job["error"] = msg[-600:]
            job["status"] = "error"
            s["errors"] = s.get("errors", 0) + 1
        finally:
            del a
        job["finished"] = time.time()
        # Next variation of the series, if any.
        s["done"] += 1
        if sid_active(job["series"]):
            if s["left"] is not None:
                s["left"] -= 1
            if (s["left"] is None or s["left"] > 0) and s.get("errors", 0) < 3:
                try:
                    enqueue(job["series"])
                except Exception:
                    end_series(job["series"])
            else:
                end_series(job["series"])


# ── stories ─────────────────────────────────────────────────────────────────

def llm(messages, max_tokens, on_text=None, cancel=lambda: False, schema=None, temperature=0.8):
    """Stream a chat completion from the local LLM; returns the full text."""
    body = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature,
            "stream": True, "cache_prompt": True}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"schema": schema}}
    req = urllib.request.Request(LLM, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    out = []
    with improve_lock, urllib.request.urlopen(req, timeout=1800) as r:
        for line in r:
            if cancel():
                break
            line = line.strip()
            if not line.startswith(b"data:") or line == b"data: [DONE]":
                continue
            try:
                delta = json.loads(line[5:])["choices"][0]["delta"].get("content") or ""
            except Exception:
                continue
            out.append(delta)
            if on_text:
                on_text("".join(out))
    return "".join(out)


def story_path(sid):
    return STORIES / f"{sid}.json"


def story_update(sid, fn):
    with story_lock:
        f = story_path(sid)
        if not f.exists():
            return
        st = json.loads(f.read_text())
        fn(st)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(f)


def scene_set_image(sc, name):
    if sc.get("image"):
        sc["history"] = (sc.get("history") or []) + [sc["image"]]
    sc["image"] = name


def scene_series(prompt, cast, params, base=None, prev=None, chain=()):
    """Queue one image for a story scene. cast: [(name, image bytes)], base: the
    scene's current picture to iterate on (edit mode: the new image starts from it),
    prev: the freshly redrawn previous scene, for continuity. chain: later scenes
    ({scene, prompt, caption, cast, base}) to redraw one by one after this one."""
    images, lines = [], []
    if base:
        images.append(base)
        lines.append("<image1> is the current picture of this scene. Make a new version of it that follows the "
                     "description below; keep whatever the description doesn't change.")
    if prev:
        images.append(prev)
        lines.append(f"<image{len(images)}> is the previous scene of the story: keep the characters, clothing and "
                     "objects consistent with it.")
    cast = cast[:4 - len(images)]
    for name, img in cast:
        images.append(img)
        lines.append(f"{name} is shown in <image{len(images)}>.")
    if cast:
        lines.append(f"Keep {', '.join(n for n, _ in cast)} looking exactly as in their reference images.")
    ssid = uuid.uuid4().hex[:12]
    series[ssid] = {"private": {"prompt": " ".join(lines + [prompt])[:2400], "negative": "", "images": images,
                                "chain": list(chain)},
                    "params": {**params, "cast": bool(images) and not base, "scene_prompt": prompt,
                               "cast_names": [n for n, _ in cast]},
                    "left": 1, "left_total": 1, "done": 0, "queued": 0}
    enqueue(ssid)


def story_save(sid, rec):
    with story_lock:
        story_path(sid).write_text(json.dumps({"id": sid, "scenes": [], "cast": [], **rec}))


def cast_in(d):
    """Parse [{name, note, image: data URL}] from a request (max 8, images as bytes)."""
    out = []
    for c in (d.get("cast") or [])[:8]:
        name = str(c.get("name", "")).strip()[:60]
        img = str(c.get("image", ""))
        if name and img.startswith("data:image/"):
            out.append({"name": name, "note": str(c.get("note", "")).strip()[:300],
                        "image": base64.b64decode(img.split(",", 1)[1]), "mime": img[5:img.index(";")]})
    return out


def cast_msg(cast, intro):
    """An OpenAI-style multimodal user message part list showing the cast photos."""
    parts = [{"type": "text", "text": intro}]
    for i, c in enumerate(cast, 1):
        parts.append({"type": "text", "text": f"\n{i}. {c['name']}" + (f" ({c['note']})" if c["note"] else "") + ":"})
        parts.append({"type": "image_url", "image_url": {"url": f"data:{c['mime']};base64,{b64e(c['image'])}"}})
    return parts


def cast_save(story, cast):
    """Store a story's cast (callers delete old entries first)."""
    d = STORIES / story
    d.mkdir(exist_ok=True)
    entries = []
    for c in cast:
        fn = f"{uuid.uuid4().hex[:10]}.{'png' if 'png' in c['mime'] else 'jpg'}"
        (d / fn).write_bytes(c["image"])
        entries.append({"name": c["name"], "note": c["note"], "file": fn})
    story_update(story, lambda st: st.update(cast=entries))


asset_lock = threading.Lock()


def assets_index():
    f = ASSETS / "index.json"
    return json.loads(f.read_text()) if f.exists() else []


def assets_write(idx):
    tmp = ASSETS / "index.tmp"
    tmp.write_text(json.dumps(idx))
    tmp.replace(ASSETS / "index.json")


def split_title(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    m = re.match(r"\s*\**\s*TITLE:\s*(.+?)\s*\**\s*\n", text)
    if m:
        return m.group(1).strip().strip('*"'), text[m.end():].strip()
    return "", text


def story_worker():
    while True:
        jid = story_work.get()
        job, priv = jobs.get(jid), story_private.pop(jid, None)
        if not job or not priv:
            continue
        job.update(status="running", started=time.time())
        cancel = lambda: job.get("cancel")
        try:
            if job["kind"] == "story":
                user = f"Write a {priv['genre'] + ' ' if priv['genre'] else ''}story of about {priv['words']} words.\n\nIdea: {priv['idea']}"
                if priv["cast"]:
                    user = cast_msg(priv["cast"], user + "\n\nCast: use these people, animals, objects and places, called by these "
                                    "names, and describe each as it looks in its photo:")
                text = llm([{"role": "system", "content": STORY_SYSTEM}, {"role": "user", "content": user}],
                           int(priv["words"] * 1.7) + 150, on_text=lambda t: job.update(text=t), cancel=cancel)
                if cancel():
                    raise RuntimeError("stopped")
                title, body = split_title(text)
                if body.strip() == "REFUSED":
                    raise RuntimeError("refused")
                story_save(jid, {
                    "title": title or priv["idea"][:60], "text": body, "idea": priv["idea"],
                    "genre": priv["genre"], "created": job["created"], "by": "ai"})
                job["title"] = title
                if priv["cast"]:
                    cast_save(jid, priv["cast"])
            elif job["kind"] == "revise":
                # Result goes back to the editor (not saved): the browser takes it with /api/story/take.
                user = f"Instruction: {priv['instruction'] or 'improve the writing'}\n\nTITLE: {priv['title']}\n\n{priv['text']}"
                if priv["cast"]:
                    user = cast_msg(priv["cast"], user + "\n\nCast photos (describe them as they look):")
                text = llm([{"role": "system", "content": REVISE_SYSTEM.format(how=REVISE_HOW[priv["how"]])},
                            {"role": "user", "content": user}],
                           int(job["words"] * 1.7) + 200, on_text=lambda t: job.update(text=t), cancel=cancel)
                if cancel():
                    raise RuntimeError("stopped")
                title, body = split_title(text)
                if body.strip() == "REFUSED":
                    raise RuntimeError("refused")
                job.update(title=title or priv["title"], text=body)
                job["status"] = "done"
                job["finished"] = time.time()
                continue
            else:  # storyboard: story -> scenes -> image jobs
                n, cast = priv["scenes"], priv["cast"]
                names = [c["name"] for c in cast]
                system = BOARD_SYSTEM.format(n=n, style=priv["style"] or "cinematic, detailed")
                schema = BOARD_SCHEMA
                user = f"Title: {priv['title']}\n\n{priv['text']}"
                if cast:
                    system += ("\nThe story has a cast with reference photos. A cast member can be a person, an animal, an object, a vehicle, "
                               "a prop, clothing or a place. Describe each one's fixed look from its photo (for objects: shape, color, material, "
                               "distinctive details). For every scene, list in \"cast\" the names (max 4, exactly as given) of the cast members "
                               "that appear in it, objects included; they will be drawn from their photos, so refer to them by name in the image_prompt.")
                    schema = json.loads(json.dumps(BOARD_SCHEMA))
                    schema["properties"]["scenes"]["items"]["properties"]["cast"] = {
                        "type": "array", "items": {"type": "string", "enum": names}, "maxItems": 4}
                    schema["properties"]["scenes"]["items"]["required"].append("cast")
                    user = cast_msg(cast, user + "\n\nCast photos:")
                text = llm([{"role": "system", "content": system}, {"role": "user", "content": user}],
                           260 * n + 200, on_text=lambda t: job.update(progress=len(t)), cancel=cancel,
                           schema=schema, temperature=0.6)
                if cancel():
                    raise RuntimeError("stopped")
                board = json.loads(text)
                scenes = [x for x in board.get("scenes", []) if x.get("image_prompt")][:n]
                if not scenes:
                    raise RuntimeError("no scenes (refused or unreadable)")
                story = priv["story"]
                story_update(story, lambda st: st.update(characters=board.get("characters", ""), aspect=priv["aspect"], scenes=[
                    {"caption": x["caption"], "prompt": x["image_prompt"], "image": None,
                     "cast": [nm for nm in x.get("cast", []) if nm in names][:4]} for x in scenes]))
                by_name = {c["name"]: c for c in cast}
                for i, x in enumerate(scenes):
                    refs = list(dict.fromkeys(nm for nm in x.get("cast", []) if nm in by_name))[:4]
                    scene_series(x["image_prompt"][:2000], [(nm, by_name[nm]["image"]) for nm in refs],
                                 {"mode": job["mode"], "fast": priv["fast"], "aspect": priv["aspect"],
                                  "story": story, "scene": i, "caption": x["caption"][:600]})
                job["scenes"] = len(scenes)
            job["status"] = "done"
        except BaseException as e:
            job.update(status="error", error=str(e)[-300:] or type(e).__name__)
        finally:
            priv.clear()
        job["finished"] = time.time()


def sid_active(sid):
    return sid in series


def drop(jid):
    with lock:
        jobs.pop(jid, None)
        temp_data.pop(jid, None)
        if jid in order:
            order.remove(jid)


def reaper():
    while True:
        time.sleep(60)
        now = time.time()
        for jid, j in list(jobs.items()):
            short = j["mode"] == "temporary" or j.get("kind") == "revise"  # untaken results don't linger
            ttl = 120 if j["status"] in ("error", "stopped") else TEMP_TTL if short else 6 * 3600  # failed runs vanish
            if j.get("finished") and now - j["finished"] > ttl:
                drop(jid)



# ── http ────────────────────────────────────────────────────────────────────

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json", headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 60 * 1024 * 1024:
            raise ValueError("too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            return self.send(200, PAGE.read_bytes(), "text/html; charset=utf-8",
                             [("Content-Security-Policy", "default-src 'self'; img-src 'self' blob: data:; connect-src 'self' blob: data:; "
                               "style-src 'unsafe-inline'; script-src 'unsafe-inline'; frame-ancestors 'none'")])
        if path == "/api/jobs":
            with lock:
                js = [dict(jobs[j]) for j in order[:60] if j in jobs]
            active = [{"id": sid, "mode": s["params"]["mode"], "done": s["done"], "left": s["left"],
                       "prompt": s["private"]["prompt"]} for sid, s in list(series.items())]
            return self.send(200, {"jobs": js, "series": active})
        if path == "/api/stories":
            with story_lock:
                out = [json.loads(f.read_text()) for f in STORIES.glob("*.json")]
            return self.send(200, sorted(out, key=lambda x: x["created"], reverse=True))
        if path.startswith("/story-cast/"):
            sid, _, fn = path[12:].partition("/")
            f = STORIES / sid / fn
            if not sid.isalnum() or "/" in fn or ".." in fn or not f.is_file():
                return self.send(404, {"error": "not found"})
            return self.send(200, f.read_bytes(), "image/png" if fn.endswith(".png") else "image/jpeg")
        if path == "/api/assets":
            with asset_lock:
                return self.send(200, assets_index())
        if path.startswith("/asset/"):
            fn = path[7:]
            f = ASSETS / fn
            if not re.fullmatch(r"[0-9a-f]{12}\.(png|jpg)", fn) or not f.is_file():
                return self.send(404, {"error": "not found"})
            return self.send(200, f.read_bytes(), "image/png" if fn.endswith(".png") else "image/jpeg")
        if path == "/api/gallery":
            files = sorted(IMG_DIR.glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True)[:200]
            return self.send(200, [f.name for f in files])
        if path.startswith("/temp/"):
            jid = path[6:]
            data = temp_data.get(jid)
            if data is None:
                return self.send(404, {"error": "gone"})
            drop(jid)
            return self.send(200, data, "image/png")
        if path.startswith("/img/"):
            name = unquote(path[5:])
            f = IMG_DIR / name
            if "/" in name or not f.is_file():
                return self.send(404, {"error": "not found"})
            return self.send(200, f.read_bytes(), "image/png")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if self.headers.get("X-Imagine") != "1":  # forces a CORS preflight; blocks cross-site posts
            return self.send(403, {"error": "forbidden"})
        try:
            d = self.body()
        except Exception as e:
            return self.send(400, {"error": str(e)})

        if path == "/api/improve":
            return self.improve(d)
        if path.startswith("/api/asset/"):
            return self.asset(path[11:], d)
        if path.startswith("/api/story/"):
            return self.story(path[11:], d)
        if path == "/api/generate":
            return self.generate(d)
        if path == "/api/series/stop":
            for sid in (d.get("ids") or list(series)):
                s = series.get(sid)
                if s:
                    end_series(sid)
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    def improve(self, d):
        prompt = str(d.get("prompt", "")).strip()[:2000]
        if not prompt:
            return self.send(400, {"error": "write a prompt first"})
        user = prompt
        if d.get("edit"):
            user = "(This is an edit instruction for the attached image(s).)\n" + user
        hint = str(d.get("hint", "")).strip()[:500]
        if hint:
            user += "\n\nDirection for the rewrite: " + hint
        req = urllib.request.Request(LLM, data=json.dumps({
            "messages": [{"role": "system", "content": IMPROVE_SYSTEM}, {"role": "user", "content": user}],
            "temperature": 0.7, "max_tokens": 320, "cache_prompt": True}).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with improve_lock, urllib.request.urlopen(req, timeout=180) as r:
                text = json.loads(r.read())["choices"][0]["message"]["content"]
        except Exception:
            return self.send(503, {"error": "The local text model isn't running (prompt-llm service)."})
        finally:
            del user, prompt, req
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip().strip('"').strip()
        if text == "REFUSED":
            return self.send(422, {"error": "Refused."})
        return self.send(200, {"prompt": text})

    def story(self, action, d):
        mode = "normal"
        if action in ("write", "save", "pictures", "revise", "scene") and d.get("mode") == "temporary":
            return self.send(400, {"error": "Stories are saved, so they don't work in Temporary mode."})
        now, sid = time.time(), uuid.uuid4().hex[:12]
        if action == "save":
            text = str(d.get("text", "")).strip()[:40000]
            if not text:
                return self.send(400, {"error": "write something first"})
            story_save(sid, {"title": str(d.get("title", "")).strip()[:120] or "Untitled",
                             "text": text, "idea": "", "genre": "", "created": now, "by": "you"})
            cast = cast_in(d)
            if cast:
                cast_save(sid, cast)
            return self.send(200, {"id": sid})
        if action == "edit":
            # Replace a story's title/text.
            story = str(d.get("story", ""))
            title = str(d.get("title", "")).strip()[:120] or "Untitled"
            text = str(d.get("text", "")).strip()[:40000]
            if not story.isalnum() or not text:
                return self.send(400, {"error": "empty story"})
            if not story_path(story).exists():
                return self.send(404, {"error": "no story"})
            story_update(story, lambda st: st.update(title=title, text=text, edited=now))
            return self.send(200, {"ok": True})
        if action == "cast":
            # Replace a story's cast.
            story = str(d.get("story", ""))
            if not story.isalnum():
                return self.send(400, {"error": "no story"})
            if not story_path(story).exists():
                return self.send(404, {"error": "no story"})
            for f in (STORIES / story).glob("*") if (STORIES / story).is_dir() else []:
                f.unlink()
            story_update(story, lambda st: st.update(cast=[]))
            cast_save(story, cast_in(d))
            return self.send(200, {"ok": True})
        if action == "scene":
            # Redraw one scene of a picture story with an edited prompt/caption, starting
            # from its current picture when one is sent. The story JSON is updated
            # now and the new picture replaces the old (kept in history) when done.
            story = str(d.get("story", ""))
            try:
                scene = int(d.get("scene"))
            except (TypeError, ValueError):
                return self.send(400, {"error": "no scene"})
            prompt = str(d.get("prompt", "")).strip()[:2000]
            caption = str(d.get("caption", "")).strip()[:600]
            if not story.isalnum() or not prompt:
                return self.send(400, {"error": "write a prompt for the scene"})
            base = str(d.get("base", ""))
            base = b64d(base.split(",", 1)[1]) if base.startswith("data:image/") else None
            cast = [(c["name"], c["image"]) for c in cast_in(d)]
            aspect = d.get("aspect") if d.get("aspect") in ASPECTS else "landscape"
            chain = []
            for t in (d.get("then") or [])[:10]:
                b = str(t.get("base", ""))
                if str(t.get("prompt", "")).strip() and isinstance(t.get("scene"), int):
                    chain.append({"scene": t["scene"], "prompt": str(t["prompt"]).strip()[:2000],
                                  "caption": str(t.get("caption", "")).strip()[:600],
                                  "cast": [(c["name"], c["image"]) for c in cast_in(t)],
                                  "base": b64d(b.split(",", 1)[1]) if b.startswith("data:image/") else None})
            f = story_path(story)
            if not f.exists() or not 0 <= scene < len(json.loads(f.read_text()).get("scenes", [])):
                return self.send(404, {"error": "no such scene"})
            story_update(story, lambda st: st["scenes"][scene].update(
                caption=caption, prompt=prompt, cast=[n for n, _ in cast]))
            scene_series(prompt, cast, {"mode": mode, "fast": bool(d.get("fast")), "aspect": aspect,
                                        "story": story, "scene": scene, "caption": caption}, base, chain=chain)
            return self.send(200, {"ok": True})
        if action == "take":
            # Hand a finished revision to its editor, then forget it.
            job = jobs.get(str(d.get("id")))
            if not job or job.get("kind") != "revise" or job["status"] != "done":
                return self.send(404, {"error": "not ready"})
            drop(job["id"])
            return self.send(200, {"title": job.get("title", ""), "text": job.get("text", ""), "how": job["how"]})
        if action == "revise":
            text = str(d.get("text", "")).strip()[:40000]
            if not text:
                return self.send(400, {"error": "empty story"})
            how = d.get("how") if d.get("how") in REVISE_HOW else "rewrite"
            story_private[sid] = {"text": text, "title": str(d.get("title", ""))[:200], "how": how,
                                  "instruction": str(d.get("instruction", "")).strip()[:1000], "cast": cast_in(d)}
            job = {"id": sid, "kind": "revise", "mode": mode, "status": "queued", "created": now, "how": how,
                   "story": str(d.get("story", ""))[:20], "words": len(text.split()) if how == "rewrite" else 350}
            with lock:
                jobs[sid] = job
                order.insert(0, sid)
            story_work.put(sid)
            return self.send(200, {"id": sid})
        if action in ("write", "pictures"):
            job = {"id": sid, "kind": "story" if action == "write" else "storyboard", "mode": mode,
                   "status": "queued", "created": now}
            if action == "write":
                idea = str(d.get("idea", "")).strip()[:3000]
                if not idea:
                    return self.send(400, {"error": "describe the story first"})
                story_private[sid] = {"idea": idea, "genre": str(d.get("genre", "")).strip()[:200],
                                      "words": LENGTHS.get(d.get("length"), 700), "cast": cast_in(d)}
                job["words"] = story_private[sid]["words"]
                job["idea"] = idea
            else:
                text = str(d.get("text", "")).strip()[:20000]
                if not text or not d.get("story"):
                    return self.send(400, {"error": "no story"})
                story_private[sid] = {"story": str(d["story"]), "title": str(d.get("title", ""))[:200], "text": text,
                                      "scenes": max(2, min(10, int(d.get("scenes") or 6))),
                                      "style": str(d.get("style", "")).strip()[:200], "fast": bool(d.get("fast")),
                                      "aspect": d.get("aspect") if d.get("aspect") in ASPECTS else "landscape",
                                      "cast": cast_in(d)}
                job["story"] = story_private[sid]["story"]
                job["scenes_wanted"] = story_private[sid]["scenes"]
            with lock:
                jobs[sid] = job
                order.insert(0, sid)
            story_work.put(sid)
            return self.send(200, {"id": sid})
        if action == "stop":
            job = jobs.get(str(d.get("id")))
            if job and job.get("kind"):
                job["cancel"] = True
                if job["status"] == "queued":
                    story_private.pop(job["id"], None)
                    job.update(status="stopped", finished=now)
            return self.send(200, {"ok": True})
        if action == "delete":
            sid = str(d.get("id", ""))
            if sid.isalnum():
                with story_lock:
                    story_path(sid).unlink(missing_ok=True)
                    if (STORIES / sid).is_dir():
                        for f in (STORIES / sid).glob("*"):
                            f.unlink()
                        (STORIES / sid).rmdir()
            return self.send(200, {"ok": True})
        return self.send(404, {"error": "not found"})

    def asset(self, action, d):
        aid = str(d.get("id") or "")
        if aid and not re.fullmatch(r"[0-9a-f]{12}", aid):
            return self.send(400, {"error": "bad id"})
        if action == "save":  # new photo, a gallery image, or rename
            name = str(d.get("name", "")).strip()[:60] or "Untitled"
            note = str(d.get("note", "")).strip()[:300]
            with asset_lock:
                idx = assets_index()
                old = next((a for a in idx if a["id"] == aid), None) if aid else None
                if old:
                    old.update(name=name, note=note)
                else:
                    aid = uuid.uuid4().hex[:12]
                    img, gal = str(d.get("image", "")), str(d.get("gallery", ""))
                    if img.startswith("data:image/"):
                        ext = "png" if img.startswith("data:image/png") else "jpg"
                        data = b64d(img.split(",", 1)[1])
                    elif gal and "/" not in gal and (IMG_DIR / gal).is_file():
                        ext, data = "png", (IMG_DIR / gal).read_bytes()  # copied byte for byte
                    else:
                        return self.send(400, {"error": "no image"})
                    (ASSETS / f"{aid}.{ext}").write_bytes(data)
                    idx.insert(0, {"id": aid, "name": name, "note": note, "file": f"{aid}.{ext}", "created": time.time()})
                assets_write(idx)
            return self.send(200, {"id": aid})
        if action == "delete":
            with asset_lock:
                idx = assets_index()
                for a in [a for a in idx if a["id"] == aid]:
                    (ASSETS / a["file"]).unlink(missing_ok=True)
                    idx.remove(a)
                assets_write(idx)
            return self.send(200, {"ok": True})
        return self.send(404, {"error": "not found"})

    def generate(self, d):
        prompt = str(d.get("prompt", "")).strip()[:2000]
        if not prompt:
            return self.send(400, {"error": "prompt required"})
        mode = d.get("mode") if d.get("mode") in ("normal", "temporary") else "normal"
        seed = str(d.get("seed") or "").strip()
        forever = bool(d.get("forever"))
        count = max(1, min(MAX_COUNT, int(d.get("count") or 1)))
        sid = uuid.uuid4().hex[:12]
        series[sid] = {"private": {"prompt": prompt, "negative": str(d.get("negative", ""))[:1000],
                                   "images": [b64d(x.split(",", 1)[-1]) for x in (d.get("images") or [])[:4]]},
                       "params": {"mode": mode, "fast": bool(d.get("fast")),
                                  "aspect": d.get("aspect") if d.get("aspect") in ASPECTS else "square"},
                       "left": None if forever else count, "left_total": None if forever else count,
                       "done": 0, "queued": 0}
        del prompt, d
        job = enqueue(sid, int(seed) if seed.isdigit() else None)
        self.send(200, {"series": sid, "first": job["id"]})


def harden():
    # Not dumpable: no core files, and /proc/<pid>/mem, environ etc. become
    # root-only, so other processes running as this user can't read our RAM.
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE = 4
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


if __name__ == "__main__":
    harden()
    os.umask(0o077)
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    STORIES.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    DATA.chmod(0o700)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=story_worker, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
