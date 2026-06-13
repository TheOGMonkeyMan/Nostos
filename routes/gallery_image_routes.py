"""Gallery image-processing routes (ADR-050, Phase 2.2).

The /api/image/* endpoints (inpaint, harmonize, sharpen, denoise, upscale-local,
remove-bg, enhance-face), split verbatim out of
routes/gallery_routes.py::setup_gallery_routes(). The heavy image libraries
(PIL, cv2, numpy, rembg, gfpgan, realesrgan, transformers, httpx) are lazy-imported
inside each route, so they stay inline; the registrar takes only the router.
"""

import json
import logging
import os

from fastapi import HTTPException, Request

from core.database import SessionLocal, ModelEndpoint

logger = logging.getLogger(__name__)


def register_image_routes(router):
    # ---- POST /api/image/inpaint — proxy to diffusion server OR OpenAI ----
    @router.post("/api/image/inpaint")
    async def inpaint_proxy(request: Request):
        """Forward inpaint request. If the selected endpoint is OpenAI, re-shape
        the request for /v1/images/edits (multipart, inverted mask). Otherwise
        proxy through to a self-hosted diffusion server's /v1/images/inpaint."""
        import httpx
        body = await request.json()
        # Use endpoint from request body (editor dropdown) or fall back to DB lookup
        base = (body.pop("_endpoint", "") or "").rstrip("/")
        chosen_model = (body.pop("_model", "") or "").strip()
        api_key = None
        if not base:
            db = SessionLocal()
            try:
                eps = db.query(ModelEndpoint).filter(
                    ModelEndpoint.is_enabled == True,
                    ModelEndpoint.model_type == "image",
                ).all()
                if not eps:
                    raise HTTPException(400, "No image generation endpoint configured. Serve a diffusion model via Cookbook first.")
                base = eps[0].base_url.rstrip("/")
                api_key = eps[0].api_key
            finally:
                db.close()
        else:
            # Pull api_key from the matching DB row so OpenAI auth works.
            # Users may have stored base_url with/without /v1 suffix and with/without
            # trailing slash, so compare normalized forms.
            def _norm_url(u: str) -> str:
                if not u:
                    return u
                u = u.rstrip("/")
                if u.endswith("/v1"):
                    u = u[:-3]
                return u
            _target = _norm_url(base)
            db = SessionLocal()
            try:
                for ep in db.query(ModelEndpoint).all():
                    if _norm_url(ep.base_url) == _target:
                        api_key = ep.api_key
                        break
            finally:
                db.close()

        if not base.endswith("/v1"):
            base += "/v1"

        is_openai = "api.openai.com" in base

        if is_openai:
            # OpenAI path: /v1/images/edits with gpt-image-1.
            # Mask convention differs from Stable Diffusion:
            #   SD:     white pixels = regenerate, black = keep
            #   OpenAI: transparent alpha = regenerate, opaque = keep
            # So we convert the incoming PNG mask into an alpha-channel PNG.
            if not api_key:
                raise HTTPException(400, "OpenAI endpoint has no api_key stored — edit it in Endpoints settings.")
            import base64, io
            try:
                from PIL import Image
            except ImportError:
                raise HTTPException(500, "Pillow not installed on server")

            try:
                img_bytes = base64.b64decode(body["image"])
                mask_bytes = base64.b64decode(body["mask"])
                source_png = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
                mask_png = Image.open(io.BytesIO(mask_bytes)).convert("L")  # luminance
                # Build OpenAI mask: RGBA where alpha=255 means keep, 0 means regenerate.
                # SD mask: white (255) = regenerate → alpha 0.  Black (0) = keep → alpha 255.
                # RGB must be white for keep areas; start from fully-white opaque and
                # overwrite alpha so visual contents match the expected semantic.
                alpha = mask_png.point(lambda p: 255 - p)
                oa_mask = Image.new("RGBA", source_png.size, (255, 255, 255, 255))
                oa_mask.putalpha(alpha)

                src_buf = io.BytesIO()
                source_png.save(src_buf, format="PNG")
                src_buf.seek(0)
                mask_buf = io.BytesIO()
                oa_mask.save(mask_buf, format="PNG")
                mask_buf.seek(0)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(400, f"Failed to prepare OpenAI request: {e}")

            width = int(body.get("width") or 1024)
            height = int(body.get("height") or 1024)
            # gpt-image-1 only accepts 1024x1024, 1024x1536, 1536x1024 (no 'auto'
            # for edits). Pick the closest to preserve aspect, default square.
            if width > height * 1.15:
                size = "1536x1024"
            elif height > width * 1.15:
                size = "1024x1536"
            else:
                size = "1024x1024"

            files = {
                "image": ("source.png", src_buf.getvalue(), "image/png"),
                "mask": ("mask.png", mask_buf.getvalue(), "image/png"),
            }
            # Honor explicit model selection from the editor; fall back to gpt-image-1.
            # dall-e-3 has no edit endpoint — refuse it loudly so the user picks again.
            oa_model = chosen_model or "gpt-image-1"
            if "dall-e-3" in oa_model:
                raise HTTPException(400, "dall-e-3 doesn't support image edits — pick gpt-image-1 or dall-e-2")
            data = {
                "model": oa_model,
                "prompt": body.get("prompt", ""),
                "size": size,
                "n": "1",
            }
            headers = {"Authorization": f"Bearer {api_key}"}
            try:
                async with httpx.AsyncClient(timeout=120) as client:
                    r = await client.post(f"{base}/images/edits", headers=headers, data=data, files=files)
                    if r.status_code != 200:
                        raise HTTPException(r.status_code, f"OpenAI edit failed: {r.text[:300]}")
                    result = r.json()
                    raw_b64 = None
                    if result.get("data"):
                        item = result["data"][0]
                        # gpt-image-1 returns b64_json by default; dall-e-2 may return url
                        if item.get("b64_json"):
                            raw_b64 = item["b64_json"]
                        elif item.get("url"):
                            async with httpx.AsyncClient(timeout=60) as c2:
                                img_r = await c2.get(item["url"])
                                if img_r.status_code == 200:
                                    raw_b64 = base64.b64encode(img_r.content).decode()
                    if not raw_b64:
                        raise HTTPException(502, "OpenAI returned no image")

                    # OpenAI's edits API doesn't truly preserve unmasked
                    # pixels — gpt-image-1 regenerates the whole image,
                    # so even areas the user didn't mask come back
                    # slightly different. Composite the model output onto
                    # the ORIGINAL source using the user's mask, so only
                    # the masked region actually changes.
                    try:
                        generated = Image.open(io.BytesIO(base64.b64decode(raw_b64))).convert("RGBA")
                        # Match the generated image to the source dims.
                        if generated.size != source_png.size:
                            generated = generated.resize(source_png.size, Image.LANCZOS)
                        # mask_png: white = regenerate (use generated),
                        #           black = keep (use source).
                        # Composite: result = source * (1 - mask_norm) + generated * mask_norm
                        # Image.composite does exactly that with `mask`.
                        blended = Image.composite(generated, source_png, mask_png)
                        out_buf = io.BytesIO()
                        blended.save(out_buf, format="PNG")
                        return {"image": base64.b64encode(out_buf.getvalue()).decode()}
                    except Exception as comp_err:
                        # If compositing fails for any reason, fall back
                        # to the raw OpenAI output rather than blocking.
                        logger.warning(f"Inpaint compose failed, returning raw: {comp_err}")
                        return {"image": raw_b64}
            except httpx.TimeoutException:
                raise HTTPException(504, "OpenAI inpaint timed out (120s)")

        # Self-hosted diffusion server path
        try:
            # Forward chosen_model so the diffusion server can route if it ever
            # supports multiple models per process. Harmless if ignored.
            if chosen_model:
                body["model"] = chosen_model
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(f"{base}/images/inpaint", json=body)
                if r.status_code != 200:
                    raise HTTPException(r.status_code, f"Inpaint failed: {r.text[:200]}")
                return r.json()
        except httpx.TimeoutException:
            raise HTTPException(504, "Inpaint request timed out (120s)")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(502, f"Inpaint error: {str(e)}")

    # ---- POST /api/image/harmonize — proper img2img call ----
    # Earlier version routed through inpaint with a full-white mask, but
    # most backends interpret "100% mask coverage" as "regenerate from
    # scratch using the prompt", ignoring the source. Real img2img sends
    # the image alongside a `strength` (denoising strength) and the model
    # mixes that fraction of new noise into the existing pixels.
    @router.post("/api/image/harmonize")
    async def harmonize_image(request: Request):
        """Harmonize = img2img. The model preserves (1 - strength) of the
        original and regenerates `strength` fraction. With strength ~0.4
        you get edge blending + lighting unification while keeping the
        composition recognisable."""
        import httpx, base64 as _b64
        body = await request.json()

        image_b64 = body.get("image")
        if not image_b64:
            raise HTTPException(400, "No image provided")

        endpoint = (body.get("_endpoint") or "").rstrip("/")
        model = (body.get("_model") or "").strip()

        base = endpoint
        api_key = None
        if not base:
            db = SessionLocal()
            try:
                eps = db.query(ModelEndpoint).filter(
                    ModelEndpoint.is_enabled == True,
                    ModelEndpoint.model_type == "image",
                ).all()
                if not eps:
                    raise HTTPException(400, "No image generation endpoint configured.")
                base = eps[0].base_url.rstrip("/")
                api_key = eps[0].api_key
            finally:
                db.close()
        else:
            db = SessionLocal()
            try:
                for ep in db.query(ModelEndpoint).all():
                    if ep.base_url.rstrip("/").rstrip("/v1") == base.rstrip("/v1"):
                        api_key = ep.api_key
                        break
            finally:
                db.close()

        if not base.endswith("/v1"):
            base += "/v1"

        prompt = body.get("prompt") or "natural lighting, harmonious color, seamless blend"
        # Legacy single-strength control (old clients) → maps to color_match
        strength = body.get("strength", 0.45)
        try:
            strength = float(strength)
        except Exception:
            strength = 0.45
        strength = max(0.05, min(0.95, strength))
        # New two-stage controls. Clients may send either color_match/seam_fix
        # explicitly, or fall back to strength→color_match for legacy.
        try:
            color_match = float(body.get("color_match", strength))
        except Exception:
            color_match = strength
        try:
            seam_fix = float(body.get("seam_fix", 0.0))
        except Exception:
            seam_fix = 0.0
        color_match = max(0.0, min(1.0, color_match))
        seam_fix = max(0.0, min(1.0, seam_fix))
        body_mask_b64 = body.get("body_mask") or body.get("mask")
        seam_mask_b64 = body.get("seam_mask")

        # OpenAI's image API has no img2img mode — its edits endpoint
        # regenerates pixels from the prompt rather than preserving the
        # source. Earlier hack (alpha-blend the regen back at `strength`)
        # produced visibly broken results, so we refuse and tell the
        # user to spin up a real diffusion endpoint instead.
        if "api.openai.com" in base:
            raise HTTPException(400,
                "Harmonize needs a diffusion server that supports img2img "
                "(SD WebUI / Forge / Comfy). OpenAI's API doesn't expose "
                "one. Cookbook → Models can serve an SD-compatible model "
                "locally in a few clicks.")

        # Try img2img-shaped routes in order. Most self-hosted servers
        # expose at least one of these. Whatever returns 200 wins.
        # /images/harmonize is our own diffusion_server.py's native endpoint —
        # try it first since it's purpose-built for this and tolerates models
        # that only ship an inpaint pipeline.
        harmonize_payload = {
            "image": image_b64,
            "prompt": prompt,
            "color_match": color_match,
            "seam_fix": seam_fix,
            # Legacy field names so an un-restarted older diffusion server
            # still recognises the body mask. The new server prefers
            # `body_mask` over `mask`, so sending both is safe.
            "strength": color_match,
        }
        if body_mask_b64:
            harmonize_payload["body_mask"] = body_mask_b64
            harmonize_payload["mask"] = body_mask_b64
        if seam_mask_b64:
            harmonize_payload["seam_mask"] = seam_mask_b64

        candidates = [
            ("/images/harmonize", "json", harmonize_payload),
            ("/images/img2img", "json", {
                "image": image_b64,
                "prompt": prompt,
                "strength": strength,
                **({"model": model} if model else {}),
            }),
            ("/images/variations", "json", {
                "image": image_b64,
                "prompt": prompt,
                "strength": strength,
                **({"model": model} if model else {}),
            }),
            # Last-resort fallback: AUTOMATIC1111-style sdapi route.
            ("/sdapi/v1/img2img", "json_a1111", {
                "init_images": [f"data:image/png;base64,{image_b64}"],
                "prompt": prompt,
                "denoising_strength": strength,
                "steps": 30,
                **({"override_settings": {"sd_model_checkpoint": model}} if model else {}),
            }),
        ]

        # Strip the /v1 for the AUTOMATIC1111 path which uses /sdapi/v1/...
        base_root = base[:-3] if base.endswith("/v1") else base

        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        last_err = None
        # Cold-start SDXL inpaint can take 60-90s on first request (loading
        # weights to GPU). 240s gives headroom for both that and a full
        # 1024×1024 inference pass on slower setups.
        async with httpx.AsyncClient(timeout=240) as client:
            for path, kind, payload in candidates:
                target = base_root + path if path.startswith("/sdapi") else base + path
                try:
                    r = await client.post(target, json=payload, headers=headers)
                    if r.status_code == 404:
                        last_err = f"{path}: 404"
                        continue  # try next variant
                    if r.status_code != 200:
                        last_err = f"{path}: {r.status_code} {r.text[:120]}"
                        continue
                    data = r.json()
                    # Normalise return shape.
                    if isinstance(data, dict):
                        # Server returned 200 with an explicit error field —
                        # surface it now instead of trying the other routes
                        # (otherwise the real error gets buried under 404s).
                        if data.get("error") and not data.get("image"):
                            raise HTTPException(502,
                                f"Diffusion server error at {path}: {data['error']}")
                        if data.get("image"):
                            return {"image": data["image"]}
                        if data.get("images") and isinstance(data["images"], list):
                            img0 = data["images"][0]
                            if isinstance(img0, str):
                                # A1111 sometimes returns "data:image/png;base64,..." prefix
                                if img0.startswith("data:"):
                                    img0 = img0.split(",", 1)[1]
                                return {"image": img0}
                        # OpenAI-style {"data":[{"b64_json": ...}]}
                        if data.get("data"):
                            item = data["data"][0]
                            if item.get("b64_json"):
                                return {"image": item["b64_json"]}
                            if item.get("url"):
                                async with httpx.AsyncClient(timeout=60) as c2:
                                    ir = await c2.get(item["url"])
                                    if ir.status_code == 200:
                                        return {"image": _b64.b64encode(ir.content).decode()}
                    last_err = f"{path}: server returned no image"
                except httpx.ConnectError as e:
                    raise HTTPException(502, f"Can't reach diffusion server at {base}: {e}")
                except httpx.TimeoutException:
                    raise HTTPException(504, "Harmonize timed out (240s) — restart the diffusion server or lower Color match / disable Seam fix")
        raise HTTPException(502,
            f"None of the img2img routes worked on {base}. "
            f"Last response: {last_err or 'unknown'}. "
            "Your diffusion server needs to expose one of /v1/images/harmonize, "
            "/v1/images/img2img, /v1/images/variations, or /sdapi/v1/img2img.")

    # ---- POST /api/image/sharpen ----
    @router.post("/api/image/sharpen")
    async def sharpen_image(request: Request):
        """Apply unsharp-mask sharpening to an image."""
        body = await request.json()
        image_b64 = body.get("image")
        amount = body.get("amount", 50) / 100.0

        from PIL import Image, ImageFilter
        import base64, io

        img_bytes = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

        # Unsharp mask: radius=2, percent=amount*200, threshold=3
        sharpened = img.filter(ImageFilter.UnsharpMask(radius=2, percent=int(amount * 200), threshold=3))

        buf = io.BytesIO()
        sharpened.save(buf, format="PNG")
        return {"image": base64.b64encode(buf.getvalue()).decode()}

    # ---- POST /api/image/denoise ----
    # AI denoise via Real-ESRGAN with the realesr-general-x4v3 weights at
    # outscale=1 + denoise_strength. Falls back to a "package missing"
    # error so the client can prompt the user to install via Cookbook.
    @router.post("/api/image/denoise")
    async def denoise_image(request: Request):
        body = await request.json()
        image_b64 = body.get("image")
        if not image_b64:
            raise HTTPException(400, "No image provided")
        try:
            strength = float(body.get("strength", 0.5))
        except Exception:
            strength = 0.5
        strength = max(0.0, min(1.0, strength))
        try:
            import base64, io
            from PIL import Image
            import numpy as np
        except ImportError as e:
            raise HTTPException(500, f"Server missing dependency: {e}")
        # Decode source image (RGB; Real-ESRGAN doesn't preserve alpha).
        img_bytes = base64.b64decode(image_b64)
        src = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        try:
            from realesrgan import RealESRGANer
        except ImportError:
            return {"error": "realesrgan not installed. Install it from Cookbook → Dependencies (search 'realesrgan')."}
        try:
            # General-purpose lightweight model with denoise control.
            from realesrgan.archs.srvgg_arch import SRVGGNetCompact
            model = SRVGGNetCompact(num_in_ch=3, num_out_ch=3, num_feat=64,
                                    num_conv=32, upscale=4, act_type='prelu')
            upsampler = RealESRGANer(
                scale=4,
                model_path='https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.5.0/realesr-general-x4v3.pth',
                dni_weight=[strength, 1.0 - strength],
                model=model,
                tile=400, tile_pad=10, pre_pad=0, half=False,
            )
            arr = np.array(src)
            output, _ = upsampler.enhance(arr, outscale=1)
            out_img = Image.fromarray(output)
            buf = io.BytesIO()
            out_img.save(buf, format="PNG")
            return {"image": base64.b64encode(buf.getvalue()).decode()}
        except Exception as e:
            logger.warning(f"Denoise failed: {e}")
            return {"error": f"Denoise failed: {e}"}

    # ---- POST /api/image/upscale-local ----
    # Local Real-ESRGAN upscale (2× or 4×). Self-contained — no diffusion
    # server required. Used by the editor's AI Upscale button.
    @router.post("/api/image/upscale-local")
    async def upscale_image_local(request: Request):
        body = await request.json()
        image_b64 = body.get("image")
        if not image_b64:
            raise HTTPException(400, "No image provided")
        try:
            scale = int(body.get("scale", 2))
        except Exception:
            scale = 2
        scale = 2 if scale not in (2, 4) else scale
        try:
            import base64, io
            from PIL import Image
            import numpy as np
        except ImportError as e:
            raise HTTPException(500, f"Server missing dependency: {e}")
        img_bytes = base64.b64decode(image_b64)
        src = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        try:
            from basicsr.archs.rrdbnet_arch import RRDBNet
            from realesrgan import RealESRGANer
        except ImportError:
            return {"error": "realesrgan not installed. Install it from Cookbook → Dependencies (search 'realesrgan')."}
        try:
            model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                            num_block=23, num_grow_ch=32, scale=4)
            upsampler = RealESRGANer(
                scale=4,
                model_path='https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth',
                model=model,
                tile=400, tile_pad=10, pre_pad=0, half=False,
            )
            arr = np.array(src)
            output, _ = upsampler.enhance(arr, outscale=scale)
            out_img = Image.fromarray(output)
            buf = io.BytesIO()
            out_img.save(buf, format="PNG")
            return {"image": base64.b64encode(buf.getvalue()).decode()}
        except Exception as e:
            logger.warning(f"Upscale failed: {e}")
            return {"error": f"Upscale failed: {e}"}

    # ---- POST /api/image/remove-bg ----
    @router.post("/api/image/remove-bg")
    async def remove_background(request: Request):
        """Remove background from an image. If the client passes a `hint_mask`
        (white-where-the-user-wants-the-subject PNG, same dims as the
        image), we constrain the output:

          1. Crop the image to the mask's bounding box (with padding) so
             the model only sees the region the user cares about.
          2. Run rembg on that crop.
          3. Paste the result back at the original offset.
          4. Multiply the final alpha by the user's mask, so anything
             outside the hint becomes transparent regardless of what the
             model thought was foreground.
        """
        body = await request.json()
        image_b64 = body.get("image")
        hint_b64 = body.get("hint_mask")

        from PIL import Image
        import base64, io

        img_bytes = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
        W, H = img.size

        hint = None
        bbox = None
        if hint_b64:
            try:
                hint_bytes = base64.b64decode(hint_b64)
                hint = Image.open(io.BytesIO(hint_bytes)).convert("L")
                # Resize the hint to match if dimensions disagree
                if hint.size != img.size:
                    hint = hint.resize(img.size, Image.NEAREST)
                # Bounding box of any non-zero pixel (with 8 px padding)
                bbox = hint.getbbox()
                if bbox:
                    pad = 8
                    bbox = (
                        max(0, bbox[0] - pad), max(0, bbox[1] - pad),
                        min(W, bbox[2] + pad), min(H, bbox[3] + pad),
                    )
            except Exception:
                hint = None
                bbox = None

        # Crop to the bbox if a hint was supplied so rembg sees just the
        # user's region of interest. Otherwise process the whole image.
        if bbox:
            crop = img.crop(bbox)
        else:
            crop = img

        try:
            from rembg import remove
            cut = remove(crop)
        except ImportError:
            try:
                from transformers import pipeline
                pipe = pipeline("image-segmentation", model="briaai/RMBG-1.4", trust_remote_code=True)
                mask_img = pipe(crop, return_mask=True).convert("L")
                tmp = crop.copy()
                tmp.putalpha(mask_img)
                cut = tmp
            except Exception:
                return {"error": "No background removal model available. Install rembg: pip install rembg"}

        # Compose the cropped result back into a full-size transparent canvas.
        if bbox:
            result = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            result.paste(cut, (bbox[0], bbox[1]), cut)
        else:
            result = cut.convert("RGBA")

        # Final alpha = result.alpha * hint (normalised). Anything outside
        # the user's hint is forced transparent.
        if hint is not None:
            r, g, b, a = result.split()
            # Multiply alphas — use ImageChops to stay in PIL-pure code.
            from PIL import ImageChops
            a = ImageChops.multiply(a, hint)
            result = Image.merge("RGBA", (r, g, b, a))

        # Edge cleanup (feather / grow) moved to the client so the user
        # can re-tune live without re-running the model. Server returns
        # the pristine cutout.

        buf = io.BytesIO()
        result.save(buf, format="PNG")
        return {"image": base64.b64encode(buf.getvalue()).decode()}

    # ---- POST /api/image/enhance-face ----
    @router.post("/api/image/enhance-face")
    async def enhance_face(request: Request):
        """Face/portrait enhancement. Uses GFPGAN if available, falls back to PIL."""
        body = await request.json()
        image_b64 = body.get("image")
        if not image_b64:
            raise HTTPException(400, "No image provided")

        import base64, io, tempfile, os
        from PIL import Image, ImageFilter, ImageEnhance
        import numpy as np

        img_bytes = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")

        # Try GFPGAN first (AI face restoration)
        try:
            from gfpgan import GFPGANer
            import cv2

            model_path = os.path.join(tempfile.gettempdir(), "gfpgan_models")
            os.makedirs(model_path, exist_ok=True)

            restorer = GFPGANer(
                model_path="https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth",
                upscale=1,
                arch="clean",
                channel_multiplier=2,
                bg_upsampler=None,
                model_rootpath=model_path,
            )

            img_bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
            _, _, output = restorer.enhance(
                img_bgr,
                has_aligned=False,
                only_center_face=False,
                paste_back=True,
            )

            # Convert back to RGB
            result_rgb = cv2.cvtColor(output, cv2.COLOR_BGR2RGB)
            result_img = Image.fromarray(result_rgb)

            buf = io.BytesIO()
            result_img.save(buf, format="PNG")
            return {"image": base64.b64encode(buf.getvalue()).decode()}

        except ImportError:
            # GFPGAN not available — use PIL-based enhancement (no AI, but works everywhere)
            logger.info("GFPGAN not available — using PIL enhancement fallback")
            # Multi-step enhancement: denoise → sharpen → contrast → color boost
            enhanced = img.filter(ImageFilter.MedianFilter(size=3))  # light denoise
            enhanced = enhanced.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))  # sharpen
            enhanced = ImageEnhance.Contrast(enhanced).enhance(1.15)  # slight contrast boost
            enhanced = ImageEnhance.Color(enhanced).enhance(1.1)  # subtle color boost
            enhanced = ImageEnhance.Brightness(enhanced).enhance(1.05)  # slight brightness lift

            buf = io.BytesIO()
            enhanced.save(buf, format="PNG")
            return {"image": base64.b64encode(buf.getvalue()).decode(), "method": "pil"}
        except Exception as e:
            raise HTTPException(500, f"Face enhancement failed: {str(e)}")
