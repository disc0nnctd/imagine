# Imagine

A small self-hosted web UI for **Qwen-Image 2.1** running in ComfyUI on a
separate GPU machine, reached over SSH. It uses only the Python standard
library, plus `websocket-client` for temporary mode.

## Features

- **Generate & edit:** prompt, shape, seed, variations (a count, or keep going until you press Stop), and editing with up to 4 input images (`<image1>`…).
- **Modes:** *Normal* saves to the gallery. *Temporary* keeps the image in RAM, serves it once, and leaves nothing on the render machine.
- **Prompt improver:** rewrites a prompt with a local LLM (any OpenAI-compatible `llama-server`).
- **Stories:** the LLM writes a story, or you write one. Stories can have a cast of named reference photos.
- **Picture stories:** the LLM plans scenes with fixed character looks, then draws each scene with its cast photos as references. Each scene can be edited (words, prompt, cast) and redrawn on its own, or with every later scene chained so changes carry forward. Earlier versions are kept.
- **Library:** reusable named reference photos. ★ on any gallery image saves it; "+ From library" works in the edit box and in cast editors.

## Files

| File | What |
|---|---|
| `server.py` | HTTP server and job queue (listens on 127.0.0.1:8790) |
| `index.html` | the whole front end |
| `qwen-image` | CLI + library that drives ComfyUI over an SSH tunnel (`status`/`start`/`stop`, render) |

## Setup

1. On the GPU machine: ComfyUI with the ComfyUI-GGUF nodes, the Qwen-Image 2.1 GGUF, its text encoder and VAE (names are in `qwen-image`), bound to localhost:8188.
2. Here: key-based SSH to that machine, then:

```bash
export QWEN_IMAGE_HOST=user@gpu-box           # ssh target
export IMAGINE_GALLERY=~/Pictures/imagine     # optional
export IMAGINE_LLM=http://127.0.0.1:8084/v1/chat/completions   # optional, for Improve + Stories
pip install websocket-client
./server.py
```

Put it behind something private, for example `tailscale serve --bg --https=8790 http://127.0.0.1:8790`. The app has **no authentication** of its own.

Data lives in `~/.local/share/imagine/` (stories, library) and in the gallery folder.
