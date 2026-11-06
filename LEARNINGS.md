# What building Imagine taught us

Imagine was a small self-hosted UI for Qwen-Image 2.1 on ComfyUI, running on a 6 GB GTX 1060 reached over SSH. These are the lessons worth keeping.

## Planning and prompts
- **One local LLM call beats two.** Picking reference photos and writing the prompt in a single call (references placed before the scene in the output schema) was faster than a separate selection pass, because the small model re-reads its prompt each time.
- **Put the instruction you care about last.** A small local model copied the clothes from reference photos until a short reminder ("invent new outfits, distinct events") was moved to after the photo inventory. Treat reference clothes, pose and background as evidence to ignore unless the user asks.
- **Instruction-following limits remain.** One shot in six still copied the source jacket. Let the user review and edit every plan before drawing.
- **Validate model output, never patch it silently.** Reject partial shot lists and out-of-range reference indices instead of inventing fallback prompts.
- **Give each reference a role, a subject name and a reason.** It makes galleries reviewable, and separate subject names let differently named files resolve to one person.

## Rendering
- Cap attached images (4 per drawing) and vision inputs (8 undescribed photos). Send already-described photos as text only.
- Chained redraws must carry quality/size metadata forward, or the chain silently drops to defaults.
- The model needs latent-aligned sizes, but the final resize should honour the exact size requested (1000 px came back as 992 before the fix).

## Operations
- The GPU box has one job at a time. A crashed test model plus a stray ComfyUI launch left ComfyUI starved, which produced NaN VAE casts and black pictures. Check what is running before starting anything, and restart ComfyUI after a model swap.
- The app had no authentication of its own. Keep it on a private network (for example `tailscale serve`).
- "Temporary" mode (image held in RAM, served once, nothing left on the render machine) was cheap to build and worth having.

## Testing
- Test with a fake renderer and record its arguments; it exercises the whole app without a GPU.
- A test that fails under the page's content-security policy may be a harness bug (string `evaluate` vs function predicates), not an app bug. Do not weaken the policy to make it pass.
