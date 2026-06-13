"""Gallery routes — browsable library for photos and AI-generated images."""

import os
import hashlib
import logging
from typing import Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from core.database import SessionLocal, GalleryImage, GalleryAlbum, ModelEndpoint
from core.database import Session as DbSession
from src.auth_helpers import get_current_user
from routes.gallery_image_routes import register_image_routes

from routes.gallery_helpers import (
    GalleryPatch, _extract_exif, _image_to_dict, _owner_filter, _human_size,
)

logger = logging.getLogger(__name__)

def setup_gallery_routes() -> APIRouter:
    router = APIRouter(tags=["gallery"])

    # ---- POST /api/gallery/upload ----
    @router.post("/api/gallery/upload")
    async def gallery_upload(request: Request):
        """Upload an image file to the gallery with EXIF extraction and dedup."""
        import uuid
        from pathlib import Path

        form = await request.form()
        file = form.get("file")
        if not file or not hasattr(file, 'filename'):
            raise HTTPException(400, "No file provided")

        user = get_current_user(request)
        album_id = form.get("album_id") or None
        content = await file.read()

        # Duplicate detection via SHA-256
        file_hash = hashlib.sha256(content).hexdigest()
        db = SessionLocal()
        try:
            # SECURITY: scope the dup-detect to THIS user — otherwise a
            # caller can probe whether someone else uploaded the same
            # file (the response leaks the existing row's id+filename).
            _dup_q = db.query(GalleryImage).filter(
                GalleryImage.file_hash == file_hash,
                GalleryImage.is_active == True,
            )
            if user:
                _dup_q = _dup_q.filter(GalleryImage.owner == user)
            existing = _dup_q.first()
            if existing:
                return {"ok": False, "duplicate": True, "filename": existing.filename,
                        "id": existing.id, "message": "Duplicate photo skipped"}

            img_dir = Path("data/generated_images")
            img_dir.mkdir(parents=True, exist_ok=True)

            ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "png"
            VIDEO_EXTS = {"mp4", "mov", "webm", "mkv", "m4v"}
            IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
            if ext not in VIDEO_EXTS and ext not in IMAGE_EXTS:
                raise HTTPException(400, f"Unsupported file type: .{ext}")
            is_video = ext in VIDEO_EXTS
            filename = f"{uuid.uuid4().hex[:12]}.{ext}"
            img_path = img_dir / filename
            img_path.write_bytes(content)

            # Extract EXIF for images only — PIL can't parse video containers
            # and the failure path logs a noisy WARNING. We'll add ffprobe-based
            # video metadata extraction in a follow-up.
            exif = {} if is_video else _extract_exif(content)
            original_name = file.filename.rsplit(".", 1)[0] if "." in file.filename else file.filename

            img_id = str(uuid.uuid4())
            db.add(GalleryImage(
                id=img_id,
                filename=filename,
                prompt=original_name,
                model="imported",
                owner=user,
                file_hash=file_hash,
                file_size=len(content),
                width=exif.get("width"),
                height=exif.get("height"),
                taken_at=exif.get("taken_at"),
                camera_make=exif.get("camera_make"),
                camera_model=exif.get("camera_model"),
                gps_lat=exif.get("gps_lat"),
                gps_lng=exif.get("gps_lng"),
                album_id=album_id,
            ))
            db.commit()
            resp = {"ok": True, "filename": filename, "id": img_id}
            if exif.get("exif_error"):
                resp["exif_warning"] = exif["exif_error"]
            return resp
        finally:
            db.close()

    # ---- POST /api/gallery/{id}/replace ----
    @router.post("/api/gallery/{image_id}/replace")
    async def gallery_replace(request: Request, image_id: str):
        """Replace an existing gallery image file with a new one."""
        from pathlib import Path

        user = get_current_user(request)
        db = SessionLocal()
        try:
            img = db.query(GalleryImage).filter(GalleryImage.id == image_id).first()
            if not img:
                raise HTTPException(404, "Image not found")
            if not user or img.owner != user:
                raise HTTPException(403, "Not your image")

            form = await request.form()
            file = form.get("image")
            if not file or not hasattr(file, 'read'):
                raise HTTPException(400, "No image provided")

            content = await file.read()
            img_dir = Path("data/generated_images")
            img_dir.mkdir(parents=True, exist_ok=True)
            img_path = img_dir / img.filename
            img_path.write_bytes(content)

            # Refresh dimensions in case the editor resized the canvas.
            # updated_at auto-bumps via TimestampMixin's onupdate hook.
            try:
                from PIL import Image
                from io import BytesIO
                with Image.open(BytesIO(content)) as new_im:
                    img.width = new_im.width
                    img.height = new_im.height
            except Exception:
                pass
            try:
                db.commit()
            except Exception as e:
                db.rollback()
                raise HTTPException(500, f"DB commit failed: {e}")
            return {"ok": True, "width": img.width, "height": img.height}
        finally:
            db.close()

    # ---- POST /api/gallery/{image_id}/rename ----
    @router.post("/api/gallery/{image_id}/rename")
    async def gallery_rename(request: Request, image_id: str):
        """Rename a gallery photo. Stores the new name in the `prompt`
        column (which serves as the user-facing label for uploaded
        photos that have no AI prompt)."""
        user = get_current_user(request)
        data = await request.json()
        new_name = (data.get("name") or "").strip()
        if not new_name:
            raise HTTPException(400, "Name cannot be empty")
        if len(new_name) > 500:
            raise HTTPException(400, "Name too long")
        db = SessionLocal()
        try:
            img = db.query(GalleryImage).filter(GalleryImage.id == image_id).first()
            if not img:
                raise HTTPException(404, "Image not found")
            if not user or img.owner != user:
                raise HTTPException(403, "Not your image")
            img.prompt = new_name
            db.commit()
            return {"ok": True, "name": new_name}
        finally:
            db.close()

    # ---- POST /api/gallery/{image_id}/rotate ----
    @router.post("/api/gallery/{image_id}/rotate")
    async def gallery_rotate(request: Request, image_id: str):
        """Rotate an image by ±90° or 180°. Updates the file on disk and the
        width/height in the DB. Body: {angle: 90 | -90 | 180}."""
        from pathlib import Path
        from PIL import Image
        from io import BytesIO

        data = await request.json()
        try:
            angle = int(data.get("angle", 90))
        except (TypeError, ValueError):
            raise HTTPException(400, "Invalid angle")
        if angle not in (90, -90, 180, 270):
            raise HTTPException(400, "Angle must be 90, -90, 180, or 270")

        user = get_current_user(request)
        db = SessionLocal()
        try:
            img = db.query(GalleryImage).filter(GalleryImage.id == image_id).first()
            if not img:
                raise HTTPException(404, "Image not found")
            if not user or img.owner != user:
                raise HTTPException(403, "Not your image")

            img_path = Path("data/generated_images") / img.filename
            if not img_path.exists():
                raise HTTPException(404, "Image file not found")

            # PIL rotates counter-clockwise; the API takes "clockwise"
            # convention so we negate to match user expectation.
            with Image.open(img_path) as pil:
                rotated = pil.rotate(-angle, expand=True)
                # Recompute hash so dedupe stays accurate.
                buf = BytesIO()
                ext = img.filename.rsplit(".", 1)[-1].lower()
                save_kwargs = {}
                if ext in ("jpg", "jpeg"):
                    save_kwargs["quality"] = 95
                    fmt = "JPEG"
                elif ext == "webp":
                    fmt = "WEBP"
                    save_kwargs["quality"] = 95
                else:
                    fmt = "PNG"
                rotated.save(buf, format=fmt, **save_kwargs)
                content = buf.getvalue()
                img_path.write_bytes(content)
                img.file_hash = hashlib.sha256(content).hexdigest()
                img.file_size = len(content)
                img.width, img.height = rotated.size
            db.commit()
            return {"ok": True, "width": img.width, "height": img.height}
        finally:
            db.close()

    # ---- POST /api/gallery/ai-upscale ----
    @router.post("/api/gallery/ai-upscale")
    async def gallery_ai_upscale(request: Request):
        """AI upscale using img2img with the diffusion server."""
        import base64, httpx

        form = await request.form()
        file = form.get("image")
        if not file: raise HTTPException(400, "No image")
        scale = int(form.get("scale", "2"))

        image_bytes = await file.read()
        b64 = base64.b64encode(image_bytes).decode()

        # Find image endpoint
        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(ModelEndpoint.model_type == "image", ModelEndpoint.is_enabled == True).first()
        finally:
            db.close()

        if not ep:
            raise HTTPException(400, "No image generation endpoint configured. Add one in Settings → Add Models.")

        base_url = ep.base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"

        # Use img2img endpoint if available, otherwise upscale via canvas on client
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(f"{base_url}/images/upscale", json={
                    "image": b64, "scale": scale,
                })
                if resp.status_code == 200:
                    data = resp.json()
                    return {"image": data.get("data", [{}])[0].get("b64_json", "")}
                # Fallback: no upscale endpoint — return error
                return {"error": f"Upscale endpoint not available ({resp.status_code})"}
        except Exception as e:
            return {"error": str(e)}

    # ---- POST /api/gallery/style-transfer ----
    @router.post("/api/gallery/style-transfer")
    async def gallery_style_transfer(request: Request):
        """Style transfer using img2img with the diffusion server."""
        import base64, httpx

        form = await request.form()
        file = form.get("image")
        prompt = form.get("prompt", "")
        strength = float(form.get("strength", "0.55"))
        if not file: raise HTTPException(400, "No image")

        image_bytes = await file.read()
        b64 = base64.b64encode(image_bytes).decode()

        db = SessionLocal()
        try:
            ep = db.query(ModelEndpoint).filter(ModelEndpoint.model_type == "image", ModelEndpoint.is_enabled == True).first()
        finally:
            db.close()

        if not ep:
            raise HTTPException(400, "No image generation endpoint configured.")

        base_url = ep.base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"

        try:
            async with httpx.AsyncClient(timeout=180) as client:
                resp = await client.post(f"{base_url}/images/generations", json={
                    "prompt": prompt,
                    "image": b64,
                    "strength": strength,
                    "response_format": "b64_json",
                })
                if resp.status_code == 200:
                    data = resp.json()
                    img_data = data.get("data", [{}])[0].get("b64_json", "")
                    if img_data:
                        return {"image": img_data}
                return {"error": f"Style transfer failed ({resp.status_code})"}
        except Exception as e:
            return {"error": str(e)}

    # ---- GET /api/gallery/tags ----
    @router.get("/api/gallery/tags")
    async def gallery_tags(request: Request) -> Dict[str, Any]:
        """Return distinct tags across all active gallery images."""
        user = get_current_user(request)
        db = SessionLocal()
        try:
            q = db.query(GalleryImage.tags).filter(
                GalleryImage.is_active == True, GalleryImage.tags != None, GalleryImage.tags != ""
            )
            q = _owner_filter(q, user)
            rows = q.all()
            tag_set = set()
            for (raw,) in rows:
                for t in raw.split(","):
                    t = t.strip()
                    if t:
                        tag_set.add(t)
            return {"tags": sorted(tag_set)}
        finally:
            db.close()

    # ---- GET /api/gallery/library ----
    @router.get("/api/gallery/library")
    async def gallery_library(
        request: Request,
        search: Optional[str] = Query(None),
        tag: Optional[str] = Query(None),
        model: Optional[str] = Query(None),
        album: Optional[str] = Query(None),
        favorites: bool = Query(False),
        sort: str = Query("recent"),
        seed: Optional[int] = Query(None),
        offset: int = Query(0, ge=0),
        limit: int = Query(24, ge=1, le=100),
    ) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            # Distinct tags for filter UI
            tag_q = db.query(GalleryImage.tags).filter(
                GalleryImage.is_active == True, GalleryImage.tags != None, GalleryImage.tags != ""
            )
            tag_q = _owner_filter(tag_q, user)
            tag_rows = tag_q.all()
            all_tags = set()
            for (raw,) in tag_rows:
                for t in raw.split(","):
                    t = t.strip()
                    if t:
                        all_tags.add(t)

            # Distinct models for filter UI
            model_q = db.query(GalleryImage.model).filter(
                GalleryImage.is_active == True, GalleryImage.model != None
            )
            model_q = _owner_filter(model_q, user)
            model_rows = model_q.distinct().all()
            all_models = sorted([m for (m,) in model_rows if m])

            # Base query with left join to sessions for session_name
            q = (
                db.query(GalleryImage, DbSession.name)
                .outerjoin(DbSession, GalleryImage.session_id == DbSession.id)
                .filter(GalleryImage.is_active == True)
            )
            if user is not None:
                q = q.filter(GalleryImage.owner == user)

            # Search filter (prompt + tags + ai_tags)
            if search:
                term = f"%{search}%"
                from sqlalchemy import or_
                q = q.filter(or_(
                    GalleryImage.prompt.ilike(term),
                    GalleryImage.tags.ilike(term),
                    GalleryImage.ai_tags.ilike(term),
                ))

            # Tag filter. The UI stacks multiple tag pills by passing them
            # comma-separated — each tag adds a separate AND-filter so the
            # result set narrows as the user piles tags on. A single tag
            # (no commas) is the original behaviour.
            if tag:
                from sqlalchemy import or_ as _or
                for one in (t.strip() for t in tag.split(",")):
                    if not one:
                        continue
                    q = q.filter(_or(
                        GalleryImage.tags.ilike(f"%{one}%"),
                        GalleryImage.ai_tags.ilike(f"%{one}%"),
                    ))

            # Model filter
            if model:
                q = q.filter(GalleryImage.model == model)

            # Album filter
            if album:
                q = q.filter(GalleryImage.album_id == album)

            # Favorites filter
            if favorites:
                q = q.filter(GalleryImage.favorite == True)

            # Total before pagination
            total = q.count()
            # How many of those have AI tags — surfaced as "X/Y photos tagged"
            # in the AI-tagging settings header.
            total_tagged = q.filter(
                GalleryImage.ai_tags.isnot(None), GalleryImage.ai_tags != ""
            ).count()

            # Sorting
            if sort == "shuffle":
                # Seeded shuffle: fetch all matching IDs, shuffle them
                # deterministically with `seed`, then re-query for just the
                # page we want. Stable across pagination as long as the
                # client keeps the same seed.
                import random as _random
                id_rows = q.with_entities(GalleryImage.id).all()
                all_ids = [r[0] for r in id_rows]
                rng = _random.Random(seed if seed is not None else 0)
                rng.shuffle(all_ids)
                page_ids = all_ids[offset:offset + limit]
                if page_ids:
                    page_rows = (
                        db.query(GalleryImage, DbSession.name)
                        .outerjoin(DbSession, GalleryImage.session_id == DbSession.id)
                        .filter(GalleryImage.id.in_(page_ids))
                        .all()
                    )
                    # Restore the shuffled order
                    by_id = {img.id: (img, session_name) for img, session_name in page_rows}
                    rows = [by_id[i] for i in page_ids if i in by_id]
                else:
                    rows = []
            else:
                if sort == "oldest":
                    q = q.order_by(GalleryImage.created_at.asc())
                else:  # recent
                    q = q.order_by(GalleryImage.created_at.desc())
                rows = q.offset(offset).limit(limit).all()

            items = []
            for img, session_name in rows:
                items.append(_image_to_dict(img, session_name))

            return {
                "items": items,
                "total": total,
                "total_tagged": total_tagged,
                "tags": sorted(all_tags),
                "models": all_models,
            }
        except Exception as e:
            logger.error(f"Failed to fetch gallery library: {e}")
            raise HTTPException(500, f"Failed to fetch gallery library: {e}")
        finally:
            db.close()

    # ---- Album CRUD (must be before {image_id} catch-all) ----

    @router.get("/api/gallery/albums")
    async def list_albums(request: Request):
        user = get_current_user(request)
        db = SessionLocal()
        try:
            q = db.query(GalleryAlbum)
            if user:
                q = q.filter(GalleryAlbum.owner == user)
            albums = q.order_by(GalleryAlbum.created_at.desc()).all()
            result = []
            for a in albums:
                count = db.query(GalleryImage).filter(
                    GalleryImage.album_id == a.id, GalleryImage.is_active == True
                ).count()
                cover_url = None
                if a.cover_id:
                    cover = db.query(GalleryImage).filter(GalleryImage.id == a.cover_id).first()
                    if cover:
                        cover_url = f"/api/generated-image/{cover.filename}"
                elif count > 0:
                    first = db.query(GalleryImage).filter(
                        GalleryImage.album_id == a.id, GalleryImage.is_active == True
                    ).order_by(GalleryImage.created_at.desc()).first()
                    if first:
                        cover_url = f"/api/generated-image/{first.filename}"
                result.append({
                    "id": a.id, "name": a.name, "description": a.description or "",
                    "cover_url": cover_url, "count": count,
                    "created_at": a.created_at.isoformat() if a.created_at else None,
                })
            return {"albums": result}
        finally:
            db.close()

    @router.post("/api/gallery/albums")
    async def create_album(request: Request):
        import uuid
        user = get_current_user(request)
        data = await request.json()
        name = (data.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "Album name required")
        db = SessionLocal()
        try:
            a = GalleryAlbum(
                id=str(uuid.uuid4()), name=name,
                description=data.get("description", ""),
                owner=user,
            )
            db.add(a)
            db.commit()
            return {"ok": True, "id": a.id, "name": a.name}
        finally:
            db.close()

    @router.get("/api/gallery/stats")
    async def gallery_stats(request: Request):
        user = get_current_user(request)
        db = SessionLocal()
        try:
            from sqlalchemy import func
            base = db.query(GalleryImage).filter(GalleryImage.is_active == True)
            size_q = db.query(func.sum(GalleryImage.file_size)).filter(GalleryImage.is_active == True)
            album_q = db.query(GalleryAlbum)
            if user:
                base = base.filter(GalleryImage.owner == user)
                size_q = size_q.filter(GalleryImage.owner == user)
                album_q = album_q.filter(GalleryAlbum.owner == user)
            total = base.count()
            total_size = size_q.scalar() or 0
            fav_count = base.filter(GalleryImage.favorite == True).count()
            album_count = album_q.count()
            return {
                "total_photos": total,
                "total_size": total_size,
                "total_size_human": _human_size(total_size),
                "favorites": fav_count,
                "albums": album_count,
            }
        finally:
            db.close()

    @router.post("/api/gallery/ai-tag-batch")
    async def ai_tag_batch(
        request: Request,
        album_id: Optional[str] = Query(None),
        limit: int = Query(200),
    ):
        user = get_current_user(request)
        db = SessionLocal()
        try:
            q = db.query(GalleryImage).filter(
                GalleryImage.is_active == True,
                (GalleryImage.ai_tags == None) | (GalleryImage.ai_tags == ""),
            )
            if user:
                q = q.filter(GalleryImage.owner == user)
            if album_id:
                q = q.filter(GalleryImage.album_id == album_id)
            untagged = q.count()
            ids = [img.id for img in q.limit(max(1, min(limit, 500))).all()]
            return {"ok": True, "queued": len(ids), "total_untagged": untagged, "image_ids": ids}
        finally:
            db.close()

    # ---- GET /api/gallery/{image_id} ----
    @router.get("/api/gallery/{image_id}")
    async def get_gallery_image(request: Request, image_id: str) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            row = (
                db.query(GalleryImage, DbSession.name)
                .outerjoin(DbSession, GalleryImage.session_id == DbSession.id)
                .filter(GalleryImage.id == image_id)
                .first()
            )
            if not row:
                raise HTTPException(404, "Image not found")
            img, session_name = row
            if not user or img.owner != user:
                raise HTTPException(404, "Image not found")
            return _image_to_dict(img, session_name)
        finally:
            db.close()

    # ---- PATCH /api/gallery/{image_id} ----
    @router.patch("/api/gallery/{image_id}")
    async def patch_gallery_image(request: Request, image_id: str, req: GalleryPatch) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            img = db.query(GalleryImage).filter(GalleryImage.id == image_id).first()
            if not img:
                raise HTTPException(404, "Image not found")
            if not user or img.owner != user:
                raise HTTPException(404, "Image not found")
            if req.tags is not None:
                # Drop any tag from the user-tags field that already lives in
                # ai_tags — earlier flows wrote AI suggestions to both fields
                # and the UI showed every photo with the same chips twice.
                ai_set = {t.strip().lower() for t in (img.ai_tags or '').split(',') if t.strip()}
                cleaned = []
                seen = set()
                for raw in (req.tags or '').split(','):
                    t = raw.strip()
                    k = t.lower()
                    if not t or k in seen or k in ai_set:
                        continue
                    seen.add(k)
                    cleaned.append(t)
                img.tags = ', '.join(cleaned)
            if req.favorite is not None:
                img.favorite = req.favorite
            if req.album_id is not None:
                img.album_id = req.album_id if req.album_id else None
            db.commit()
            db.refresh(img)
            return _image_to_dict(img)
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(500, str(e))
        finally:
            db.close()

    # ---- POST /api/gallery/download-zip ----
    # Bundle the given image ids into a single .zip for download. Used by the
    # gallery's bulk "Download" when many photos are selected (one file instead
    # of a flood of individual downloads).
    @router.post("/api/gallery/download-zip")
    async def gallery_download_zip(request: Request):
        user = get_current_user(request)
        if not user:
            raise HTTPException(401, "Not authenticated")
        try:
            data = await request.json()
        except Exception:
            data = {}
        ids = data.get("ids") or []
        if not ids:
            raise HTTPException(400, "No images specified")
        db = SessionLocal()
        try:
            imgs = db.query(GalleryImage).filter(
                GalleryImage.id.in_(ids),
                GalleryImage.owner == user,
            ).all()
            if not imgs:
                raise HTTPException(404, "No images found")
            import io
            import re
            import zipfile
            buf = io.BytesIO()
            used = set()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for img in imgs:
                    src = os.path.join("data", "generated_images", img.filename)
                    if not os.path.exists(src):
                        continue
                    ext = os.path.splitext(img.filename)[1] or ".png"
                    base = (img.prompt or "").strip() or os.path.splitext(img.filename)[0]
                    base = re.sub(r"[^\w\-. ]+", "", base)[:60].strip() or img.id
                    name = f"{base}{ext}"
                    i = 1
                    while name in used:
                        name = f"{base}-{i}{ext}"
                        i += 1
                    used.add(name)
                    zf.write(src, arcname=name)
            if not used:
                raise HTTPException(404, "No image files found on disk")
            from fastapi import Response
            return Response(
                content=buf.getvalue(),
                media_type="application/zip",
                headers={"Content-Disposition": 'attachment; filename="gallery-photos.zip"'},
            )
        finally:
            db.close()

    # ---- POST /api/gallery/clear-user-tags ----
    # Wipe the `tags` field on every image owned by the current user.
    # Leaves `ai_tags` intact. Use after a bug populated user-tags with
    # AI-suggested values you never added.
    @router.post("/api/gallery/clear-user-tags")
    async def clear_gallery_user_tags(request: Request) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            q = db.query(GalleryImage).filter(GalleryImage.is_active == True)
            q = _owner_filter(q, user)
            cleared = 0
            for img in q.all():
                if img.tags:
                    img.tags = ''
                    cleared += 1
            db.commit()
            return {"ok": True, "cleared": cleared}
        except Exception as e:
            db.rollback()
            raise HTTPException(500, str(e))
        finally:
            db.close()

    # ---- POST /api/gallery/clear-ai-tags ----
    # Wipe the `ai_tags` field on every image owned by the current user.
    # Leaves user `tags` intact. Use when AI-suggested tags like "dog" /
    # "woman" have leaked into the gallery and you want them gone.
    @router.post("/api/gallery/clear-ai-tags")
    async def clear_gallery_ai_tags(request: Request, image_id: Optional[str] = Query(None)) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            q = db.query(GalleryImage).filter(GalleryImage.is_active == True)
            q = _owner_filter(q, user)
            if image_id:  # clear just one photo's AI tags
                q = q.filter(GalleryImage.id == image_id)
            cleared = 0
            for img in q.all():
                if img.ai_tags:
                    img.ai_tags = ''
                    cleared += 1
            db.commit()
            return {"ok": True, "cleared": cleared}
        except Exception as e:
            db.rollback()
            raise HTTPException(500, str(e))
        finally:
            db.close()

    # ---- POST /api/gallery/dedupe-tags ----
    # One-shot cleanup: for every image owned by the current user, drop any
    # tag from `tags` that also appears in `ai_tags` (case-insensitive).
    # Returns how many rows were touched + how many tags removed.
    @router.post("/api/gallery/dedupe-tags")
    async def dedupe_gallery_tags(request: Request) -> Dict[str, Any]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            q = db.query(GalleryImage).filter(GalleryImage.is_active == True)
            q = _owner_filter(q, user)
            rows_touched = 0
            tags_removed = 0
            for img in q.all():
                ai_set = {t.strip().lower() for t in (img.ai_tags or '').split(',') if t.strip()}
                if not ai_set:
                    continue
                original = [t.strip() for t in (img.tags or '').split(',') if t.strip()]
                cleaned = []
                seen = set()
                for t in original:
                    k = t.lower()
                    if k in ai_set or k in seen:
                        continue
                    seen.add(k)
                    cleaned.append(t)
                if len(cleaned) != len(original):
                    rows_touched += 1
                    tags_removed += len(original) - len(cleaned)
                    img.tags = ', '.join(cleaned)
            db.commit()
            return {"ok": True, "rows_touched": rows_touched, "tags_removed": tags_removed}
        except Exception as e:
            db.rollback()
            raise HTTPException(500, str(e))
        finally:
            db.close()

    # ---- DELETE /api/gallery/{image_id} ----
    @router.delete("/api/gallery/{image_id}")
    async def delete_gallery_image(request: Request, image_id: str) -> Dict[str, str]:
        user = get_current_user(request)
        db = SessionLocal()
        try:
            img = db.query(GalleryImage).filter(GalleryImage.id == image_id).first()
            if not img:
                raise HTTPException(404, "Image not found")
            if not user or img.owner != user:
                raise HTTPException(404, "Image not found")

            img_filename = img.filename
            # Remove the file from disk
            img_path = os.path.join("data", "generated_images", img_filename)
            if os.path.exists(img_path):
                os.remove(img_path)

            # Soft-delete the record
            img.is_active = False
            db.commit()

            # Strip stale chat-history references so the image bubble
            # (and its prompt caption) doesn't come back after a server
            # reboot replays the session. We remove the matching tool
            # event entirely; if that leaves the message with no other
            # tool events AND a "Generated image for: …" body, drop the
            # whole row so there's no remnant.
            try:
                from core.database import ChatMessage as _ChatMessage
                from sqlalchemy import or_ as _or
                import json as _json
                # Match by image_id OR by filename — older messages
                # (saved before we threaded image_id through the SSE)
                # only carry image_url containing the filename.
                msgs = db.query(_ChatMessage).filter(
                    _ChatMessage.meta_data.isnot(None),
                    _or(
                        _ChatMessage.meta_data.like(f"%{image_id}%"),
                        _ChatMessage.meta_data.like(f"%{img_filename}%"),
                    ),
                ).all()
                rows_to_delete = []
                for m in msgs:
                    if not m.meta_data:
                        continue
                    try:
                        meta = _json.loads(m.meta_data)
                    except Exception:
                        continue
                    events = meta.get("tool_events") or []
                    new_events = []
                    removed_any = False
                    for ev in events:
                        if not isinstance(ev, dict):
                            new_events.append(ev)
                            continue
                        is_match = ev.get("image_id") == image_id or (
                            ev.get("image_url") and img_filename in ev["image_url"]
                        )
                        if is_match:
                            removed_any = True
                            continue
                        new_events.append(ev)
                    if not removed_any:
                        continue
                    # If the message has no other tool events left, drop
                    # it AND the immediately preceding user prompt that
                    # asked for the image, so no remnant of the exchange
                    # survives.
                    if not new_events:
                        rows_to_delete.append(m)
                        prev = (
                            db.query(_ChatMessage)
                            .filter(
                                _ChatMessage.session_id == m.session_id,
                                _ChatMessage.timestamp < m.timestamp,
                            )
                            .order_by(_ChatMessage.timestamp.desc())
                            .first()
                        )
                        if prev and prev.role == "user":
                            prev_meta = {}
                            try:
                                prev_meta = _json.loads(prev.meta_data) if prev.meta_data else {}
                            except Exception:
                                prev_meta = {}
                            # Only purge the prompt if it has no tool
                            # events of its own (i.e. it's a pure user
                            # message, not an agent step).
                            if not (prev_meta.get("tool_events") or []):
                                rows_to_delete.append(prev)
                    else:
                        meta["tool_events"] = new_events
                        m.meta_data = _json.dumps(meta)
                for m in rows_to_delete:
                    db.delete(m)
                if msgs:
                    db.commit()
            except Exception as _e:
                # Cleanup is best-effort — never block the delete itself.
                logger.warning(f"chat-history cleanup after image delete failed: {_e}")

            return {"status": "deleted", "id": image_id}
        except HTTPException:
            raise
        except Exception as e:
            db.rollback()
            raise HTTPException(500, str(e))
        finally:
            db.close()

    register_image_routes(router)

    # ---- Album management (path-param routes) ----

    def _get_or_404_album(db, album_id: str, user):
        album = db.query(GalleryAlbum).filter(GalleryAlbum.id == album_id).first()
        if not album:
            raise HTTPException(404, "Album not found")
        if not user or album.owner != user:
            raise HTTPException(404, "Album not found")
        return album

    def _get_or_404_image(db, image_id: str, user):
        img = db.query(GalleryImage).filter(GalleryImage.id == image_id).first()
        if not img:
            raise HTTPException(404, "Image not found")
        if not user or img.owner != user:
            raise HTTPException(404, "Image not found")
        return img

    @router.put("/api/gallery/albums/{album_id}")
    async def update_album(request: Request, album_id: str):
        user = get_current_user(request)
        data = await request.json()
        db = SessionLocal()
        try:
            album = _get_or_404_album(db, album_id, user)
            if data.get("name") is not None:
                album.name = data["name"]
            if data.get("description") is not None:
                album.description = data["description"]
            if data.get("cover_id") is not None:
                cover_id = data["cover_id"] or None
                if cover_id:
                    _get_or_404_image(db, cover_id, user)
                album.cover_id = cover_id
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.delete("/api/gallery/albums/{album_id}")
    async def delete_album(request: Request, album_id: str):
        user = get_current_user(request)
        db = SessionLocal()
        try:
            album = _get_or_404_album(db, album_id, user)
            db.query(GalleryImage).filter(GalleryImage.album_id == album_id).update(
                {"album_id": None}, synchronize_session=False
            )
            db.delete(album)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    @router.post("/api/gallery/albums/{album_id}/add")
    async def add_to_album(request: Request, album_id: str):
        user = get_current_user(request)
        data = await request.json()
        ids = data.get("image_ids", [])
        db = SessionLocal()
        try:
            _get_or_404_album(db, album_id, user)
            # Only move images the caller owns
            q = db.query(GalleryImage).filter(GalleryImage.id.in_(ids))
            if user:
                q = q.filter(GalleryImage.owner == user)
            q.update({"album_id": album_id}, synchronize_session=False)
            db.commit()
            return {"ok": True, "count": len(ids)}
        finally:
            db.close()

    @router.post("/api/gallery/albums/{album_id}/remove")
    async def remove_from_album(request: Request, album_id: str):
        user = get_current_user(request)
        data = await request.json()
        ids = data.get("image_ids", [])
        db = SessionLocal()
        try:
            _get_or_404_album(db, album_id, user)
            q = db.query(GalleryImage).filter(
                GalleryImage.id.in_(ids), GalleryImage.album_id == album_id
            )
            if user:
                q = q.filter(GalleryImage.owner == user)
            q.update({"album_id": None}, synchronize_session=False)
            db.commit()
            return {"ok": True}
        finally:
            db.close()

    # ---- Favorite toggle ----

    @router.post("/api/gallery/{image_id}/favorite")
    async def toggle_favorite(request: Request, image_id: str):
        user = get_current_user(request)
        db = SessionLocal()
        try:
            img = _get_or_404_image(db, image_id, user)
            img.favorite = not img.favorite
            db.commit()
            return {"ok": True, "favorite": img.favorite}
        finally:
            db.close()

    # ---- AI auto-tag ----

    @router.post("/api/gallery/{image_id}/ai-tag")
    async def ai_tag_image(request: Request, image_id: str):
        """Send image to vision model for auto-tagging."""
        import base64, httpx
        from pathlib import Path

        user = get_current_user(request)
        db = SessionLocal()
        try:
            img = _get_or_404_image(db, image_id, user)

            img_path = Path("data/generated_images") / img.filename
            if not img_path.exists():
                raise HTTPException(404, "Image file not found")

            # Read and encode
            img_bytes = img_path.read_bytes()
            b64 = base64.b64encode(img_bytes).decode()
            ext = img.filename.rsplit(".", 1)[-1].lower()
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "webp": "image/webp", "gif": "image/gif"}.get(ext, "image/jpeg")

            # Resolve vision model via admin Vision setting (same resolver used for docs)
            from src.document_processor import _load_vl_settings, _resolve_vl_model
            vl_settings = _load_vl_settings()
            if not vl_settings.get("vision_enabled", True):
                return {"error": "Vision is disabled — enable it in Settings → Vision"}
            configured = vl_settings.get("vision_model", "")
            try:
                chat_url, model_name, headers = _resolve_vl_model(configured)
            except ValueError:
                return {"error": "No vision model configured — set one in Settings → Vision"}
            if not chat_url:
                return {"error": "No vision-capable endpoint configured"}

            # Call vision model — format differs between Anthropic and OpenAI
            from src.llm_core import _detect_provider
            provider = _detect_provider(chat_url)
            tag_prompt = (
                "Analyze this photo. Return ONLY a comma-separated list of tags. "
                "Include: objects, people (describe by appearance — age range, gender), "
                "scene/setting, activities, mood/atmosphere, colors, location type, "
                "time of day, weather if visible, any text/signs visible. "
                "Be specific but concise. 10-25 tags. No explanation, just tags."
            )

            if provider == "anthropic":
                payload = {
                    "model": model_name,
                    "max_tokens": 200,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {
                                "type": "base64", "media_type": mime, "data": b64,
                            }},
                            {"type": "text", "text": tag_prompt},
                        ],
                    }],
                }
            else:
                payload = {
                    "model": model_name,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": tag_prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                        ],
                    }],
                    "max_tokens": 200,
                    "temperature": 0.3,
                }

            h = {"Content-Type": "application/json"}
            if headers:
                h.update(headers)

            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(chat_url, json=payload, headers=h)
                if resp.status_code != 200:
                    body = resp.text[:500]
                    logger.error(f"Vision model {resp.status_code}: {body}")
                    return {"error": f"Vision model returned {resp.status_code}: {body[:200]}"}
                data = resp.json()
                # Anthropic returns content[0].text, OpenAI returns choices[0].message.content
                if provider == "anthropic":
                    content = (data.get("content") or [{}])[0].get("text", "")
                else:
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")

            # Clean up tags
            tags = [t.strip().lower() for t in content.split(",") if t.strip()]
            tag_str = ", ".join(tags[:30])
            img.ai_tags = tag_str
            db.commit()
            return {"ok": True, "ai_tags": tag_str}
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"AI tagging failed: {e}")
            return {"error": str(e)}
        finally:
            db.close()

    return router



