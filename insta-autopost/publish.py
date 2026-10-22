"""
Robô de publicação automática no Instagram.

Varre a pasta posts/. Cada post é uma subpasta com um post.json e os arquivos de mídia.
Quando chega a hora (scheduled_at), publica via API oficial do Instagram e marca como publicado.

Variáveis de ambiente:
  IG_ACCESS_TOKEN  - token do Instagram (segredo do GitHub)
  MEDIA_BASE_URL   - URL pública onde os arquivos da pasta posts/ ficam acessíveis
                     ex.: https://raw.githubusercontent.com/USUARIO/REPO/main/posts
  DRY_RUN          - "1" para só simular, sem publicar
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

GRAPH = "https://graph.instagram.com"
POSTS_DIR = Path(__file__).parent / "posts"
TOKEN = os.environ.get("IG_ACCESS_TOKEN", "")
MEDIA_BASE_URL = os.environ.get("MEDIA_BASE_URL", "").rstrip("/")
DRY_RUN = os.environ.get("DRY_RUN") == "1"

VIDEO_EXT = {".mp4", ".mov"}


def api(method, path, **params):
    params["access_token"] = TOKEN
    r = requests.request(method, f"{GRAPH}/{path}", params=params, timeout=60)
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"Erro da API ({r.status_code}): {data}")
    return data


def media_url(post_dir: Path, filename: str) -> str:
    return f"{MEDIA_BASE_URL}/{quote(post_dir.name)}/{quote(filename)}"


def wait_ready(container_id: str, max_wait=600):
    """Vídeos precisam ser processados pelo Instagram antes de publicar."""
    start = time.time()
    while time.time() - start < max_wait:
        status = api("GET", container_id, fields="status_code").get("status_code")
        if status == "FINISHED":
            return
        if status in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Processamento da mídia falhou: {status}")
        time.sleep(10)
    raise RuntimeError("Tempo esgotado esperando o Instagram processar a mídia")


def create_item(url: str, is_video: bool, **extra):
    if is_video:
        cid = api("POST", "me/media", video_url=url, **extra)["id"]
        wait_ready(cid)
    else:
        cid = api("POST", "me/media", image_url=url, **extra)["id"]
    return cid


def publish(post_dir: Path, post: dict) -> str:
    files = post["media"]
    caption = post.get("caption", "")
    kind = post.get("type", "auto")
    if kind == "auto":
        if len(files) > 1:
            kind = "carousel"
        elif Path(files[0]).suffix.lower() in VIDEO_EXT:
            kind = "reel"
        else:
            kind = "image"

    if kind == "image":
        cid = create_item(media_url(post_dir, files[0]), False, caption=caption)
    elif kind == "reel":
        cid = create_item(media_url(post_dir, files[0]), True, media_type="REELS", caption=caption)
    elif kind == "carousel":
        if not 2 <= len(files) <= 10:
            raise ValueError("Carrossel precisa de 2 a 10 arquivos")
        children = []
        for f in files:
            is_video = Path(f).suffix.lower() in VIDEO_EXT
            extra = {"is_carousel_item": "true"}
            if is_video:
                extra["media_type"] = "VIDEO"
            children.append(create_item(media_url(post_dir, f), is_video, **extra))
        cid = api("POST", "me/media", media_type="CAROUSEL",
                  children=",".join(children), caption=caption)["id"]
        wait_ready(cid)
    else:
        raise ValueError(f"Tipo de post desconhecido: {kind}")

    return api("POST", "me/media_publish", creation_id=cid)["id"]


def main():
    if not DRY_RUN and (not TOKEN or not MEDIA_BASE_URL):
        sys.exit("Faltam IG_ACCESS_TOKEN ou MEDIA_BASE_URL")

    now = datetime.now(timezone.utc)
    changed = False
    failures = 0

    for pj in sorted(POSTS_DIR.glob("*/post.json")):
        post = json.loads(pj.read_text(encoding="utf-8"))
        if post.get("status", "agendado") != "agendado":
            continue
        when = datetime.fromisoformat(post["scheduled_at"])
        if when.tzinfo is None:
            raise ValueError(f"{pj}: scheduled_at precisa de fuso, ex. -03:00")
        if when > now:
            print(f"Aguardando: {pj.parent.name} ({post['scheduled_at']})")
            continue

        print(f"Publicando: {pj.parent.name}")
        if DRY_RUN:
            print("  (simulação, nada foi publicado)")
            continue
        try:
            media_id = publish(pj.parent, post)
            post["status"] = "publicado"
            post["published_at"] = now.isoformat()
            post["instagram_media_id"] = media_id
            post.pop("error", None)
            print(f"  OK, id {media_id}")
        except Exception as e:  # registra o erro e não tenta de novo sozinho
            post["status"] = "erro"
            post["error"] = str(e)[:500]
            failures += 1
            print(f"  ERRO: {e}")
        pj.write_text(json.dumps(post, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        changed = True

    if not changed:
        print("Nada para publicar agora.")
    if failures:
        sys.exit(1)  # faz o GitHub te avisar por e-mail


if __name__ == "__main__":
    main()
