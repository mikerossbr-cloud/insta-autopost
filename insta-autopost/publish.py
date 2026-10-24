"""
Robô de publicação automática no Instagram (uma ou várias contas).

Varre a pasta posts/. Cada post é uma subpasta com um post.json e os arquivos de mídia.
Quando chega a hora (scheduled_at), publica via API oficial do Instagram em cada conta
listada e registra o resultado de cada uma.

Contas: arquivo contas.json, ao lado deste script. Cada conta aponta para o nome do
segredo do GitHub que guarda o token dela:
  {
    "mikerossbr3": {"secret": "IG_ACCESS_TOKEN", "padrao": true},
    "outraconta":  {"secret": "IG_TOKEN_OUTRACONTA"}
  }
No post.json, "contas": ["mikerossbr3", "outraconta"] escolhe onde publicar.
"type": "story" publica cada arquivo de "media" como um story, na ordem.
Sem esse campo, o post vai para as contas marcadas com "padrao": true.

Variáveis de ambiente:
  IG_SECRETS      - JSON com os segredos do repositório (toJSON(secrets) no workflow)
  MEDIA_BASE_URL  - URL pública onde os arquivos da pasta posts/ ficam acessíveis
  DRY_RUN         - "1" para só simular, sem publicar
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

GRAPH = os.environ.get("GRAPH_URL", "https://graph.instagram.com").rstrip("/")
BASE_DIR = Path(__file__).parent
POSTS_DIR = BASE_DIR / "posts"
CONTAS_FILE = BASE_DIR / "contas.json"
MEDIA_BASE_URL = os.environ.get("MEDIA_BASE_URL", "").rstrip("/")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "5"))
STORY_GAP_SECONDS = float(os.environ.get("STORY_GAP_SECONDS", "20"))

VIDEO_EXT = {".mp4", ".mov"}


class InstagramAPI:
    def __init__(self, token: str):
        self.token = token

    def call(self, method, path, **params):
        params["access_token"] = self.token
        r = requests.request(method, f"{GRAPH}/{path}", params=params, timeout=60)
        try:
            data = r.json() if r.content else {}
        except ValueError:
            data = {"raw": r.text[:300]}
        if r.status_code >= 400 or "error" in data:
            raise RuntimeError(f"Erro da API ({r.status_code}): {data}")
        return data

    def wait_ready(self, container_id: str, max_wait=600):
        """Espera o Instagram terminar de baixar/processar a mídia (foto ou vídeo)."""
        start = time.time()
        while time.time() - start < max_wait:
            status = self.call("GET", container_id, fields="status_code").get("status_code")
            if status in (None, "FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Processamento da mídia falhou: {status}")
            time.sleep(POLL_SECONDS)
        raise RuntimeError("Tempo esgotado esperando o Instagram processar a mídia")

    def create_item(self, url: str, is_video: bool, **extra):
        key = "video_url" if is_video else "image_url"
        cid = self.call("POST", "me/media", **{key: url}, **extra)["id"]
        self.wait_ready(cid)
        return cid

    def publish_container(self, cid: str, tries=5):
        for attempt in range(1, tries + 1):
            try:
                return self.call("POST", "me/media_publish", creation_id=cid)["id"]
            except RuntimeError as e:
                # 2207027 = "mídia ainda não está pronta": espera e tenta de novo
                if "2207027" in str(e) and attempt < tries:
                    time.sleep(POLL_SECONDS * 2)
                    continue
                raise


def media_url(post_dir: Path, filename: str) -> str:
    return f"{MEDIA_BASE_URL}/{quote(post_dir.name)}/{quote(filename)}"


def post_kind(post: dict) -> str:
    files = post["media"]
    kind = post.get("type", "auto")
    if kind == "auto":
        if len(files) > 1:
            return "carousel"
        return "reel" if Path(files[0]).suffix.lower() in VIDEO_EXT else "image"
    return kind


def publish_stories(api: InstagramAPI, post_dir: Path, files: list, feitos: list) -> str:
    """Publica cada arquivo como um story, na ordem. `feitos` guarda os ids já
    publicados, para uma nova tentativa continuar de onde parou sem repetir."""
    for i, f in enumerate(files):
        if i < len(feitos):
            continue
        is_video = Path(f).suffix.lower() in VIDEO_EXT
        cid = api.create_item(media_url(post_dir, f), is_video, media_type="STORIES")
        feitos.append(api.publish_container(cid))
        if i < len(files) - 1:
            time.sleep(STORY_GAP_SECONDS)  # garante a ordem da sequência
    return feitos[-1]


def publish(api: InstagramAPI, post_dir: Path, post: dict, feitos=None) -> str:
    files = post["media"]
    caption = post.get("caption", "")
    kind = post_kind(post)

    if kind == "story":
        return publish_stories(api, post_dir, files, feitos if feitos is not None else [])
    if kind == "image":
        cid = api.create_item(media_url(post_dir, files[0]), False, caption=caption)
    elif kind == "reel":
        cid = api.create_item(media_url(post_dir, files[0]), True,
                              media_type="REELS", caption=caption)
    elif kind == "carousel":
        if not 2 <= len(files) <= 10:
            raise ValueError("Carrossel precisa de 2 a 10 arquivos")
        children = []
        for f in files:
            is_video = Path(f).suffix.lower() in VIDEO_EXT
            extra = {"is_carousel_item": "true"}
            if is_video:
                extra["media_type"] = "VIDEO"
            children.append(api.create_item(media_url(post_dir, f), is_video, **extra))
        cid = api.call("POST", "me/media", media_type="CAROUSEL",
                       children=",".join(children), caption=caption)["id"]
        api.wait_ready(cid)
    else:
        raise ValueError(f"Tipo de post desconhecido: {kind}")

    return api.publish_container(cid)


def load_contas() -> dict:
    if CONTAS_FILE.exists():
        contas = json.loads(CONTAS_FILE.read_text(encoding="utf-8"))
    else:  # compatibilidade com a versão de uma conta só
        contas = {"principal": {"secret": "IG_ACCESS_TOKEN", "padrao": True}}
    if not contas:
        sys.exit("contas.json está vazio")
    return contas


def load_tokens() -> dict:
    raw = os.environ.get("IG_SECRETS", "")
    secrets = json.loads(raw) if raw else {}
    # permite também passar o token como variável de ambiente comum
    for k, v in os.environ.items():
        if k.startswith("IG_") and k != "IG_SECRETS":
            secrets.setdefault(k, v)
    return secrets


# --- sincronização com o repositório (só dentro do GitHub Actions) -----------------
# Evita post duplicado: cada execução busca a fila mais recente antes de publicar,
# "reserva" o post (status publicando) e salva o resultado logo em seguida.
GIT_SYNC = os.environ.get("GITHUB_ACTIONS") == "true" and not DRY_RUN
REPO_DIR = BASE_DIR.parent


def git(*args, check=True):
    return subprocess.run(["git", *args], cwd=REPO_DIR, check=check,
                          capture_output=True, text=True)


def git_sync():
    """Descarta o estado local e pega a versão mais recente da branch main."""
    if not GIT_SYNC:
        return
    git("fetch", "--quiet", "origin", "main")
    git("reset", "--quiet", "--hard", "FETCH_HEAD")


def git_save(path: Path, msg: str) -> bool:
    """Grava a mudança de um post e envia. Retorna False se não conseguiu enviar."""
    if not GIT_SYNC:
        return True
    git("add", str(path))
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        return True
    git("-c", "user.name=robo-instagram",
        "-c", "user.email=robo-instagram@users.noreply.github.com",
        "commit", "--quiet", "-m", msg)
    for _ in range(4):
        if git("push", "--quiet", "origin", "HEAD:main", check=False).returncode == 0:
            return True
        if git("pull", "--quiet", "--rebase", "origin", "main", check=False).returncode != 0:
            git("rebase", "--abort", check=False)
            return False
        time.sleep(2)
    return False


def contas_do_post(post: dict, contas: dict) -> list:
    escolhidas = post.get("contas")
    if escolhidas is None:
        escolhidas = [n for n, c in contas.items() if c.get("padrao")] or [next(iter(contas))]
    if isinstance(escolhidas, str):
        escolhidas = [escolhidas]
    return [c.lstrip("@") for c in escolhidas]


def main():
    contas = load_contas()
    tokens = load_tokens()
    if not DRY_RUN and not MEDIA_BASE_URL:
        sys.exit("Falta MEDIA_BASE_URL")

    now = datetime.now(timezone.utc)
    changed = False
    failures = 0

    git_sync()
    for pj in sorted(POSTS_DIR.glob("*/post.json")):
        post = json.loads(pj.read_text(encoding="utf-8"))
        if post.get("status", "agendado") != "agendado":
            continue
        when = datetime.fromisoformat(post["scheduled_at"])
        if when.tzinfo is None:
            print(f"ERRO {pj.parent.name}: scheduled_at precisa de fuso, ex. -03:00")
            failures += 1
            continue
        if when > now:
            print(f"Aguardando: {pj.parent.name} ({post['scheduled_at']})")
            continue

        alvos = contas_do_post(post, contas)
        print(f"Publicando: {pj.parent.name} -> {', '.join('@' + a for a in alvos)}")
        if DRY_RUN:
            print("  (simulação, nada foi publicado)")
            continue

        # confere de novo na fila mais recente e reserva o post antes de publicar
        git_sync()
        post = json.loads(pj.read_text(encoding="utf-8")) if pj.exists() else {}
        if post.get("status", "agendado") != "agendado":
            print("  já tratado por outra execução, pulando")
            continue
        post["status"] = "publicando"
        pj.write_text(json.dumps(post, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not git_save(pj, f"Publicando {pj.parent.name}"):
            print("  não consegui reservar o post (fila mudou), fica para a próxima rodada")
            git_sync()
            continue

        resultado = post.setdefault("resultado", {})
        for conta in alvos:
            if resultado.get(conta, {}).get("status") == "publicado":
                print(f"  @{conta}: já publicado antes, pulando")
                continue
            # stories já publicados numa tentativa anterior (para não repetir)
            feitos = list(resultado.get(conta, {}).get("stories_publicados", []))
            try:
                cfg = contas.get(conta)
                if not cfg:
                    raise ValueError(f"Conta '{conta}' não existe em contas.json")
                token = tokens.get(cfg["secret"])
                if not token:
                    raise ValueError(f"Segredo {cfg['secret']} não encontrado no GitHub")
                media_id = publish(InstagramAPI(token), pj.parent, post, feitos)
                resultado[conta] = {"status": "publicado", "instagram_media_id": media_id,
                                    "published_at": datetime.now(timezone.utc).isoformat()}
                if feitos:
                    resultado[conta]["stories_publicados"] = feitos
                print(f"  @{conta}: OK, id {media_id}")
            except Exception as e:  # uma conta com erro não impede as outras
                resultado[conta] = {"status": "erro", "error": str(e)[:500]}
                if feitos:
                    resultado[conta]["stories_publicados"] = feitos
                print(f"  @{conta}: ERRO: {e}")

        erros = {c: r["error"] for c, r in resultado.items()
                 if c in alvos and r.get("status") == "erro"}
        if erros:
            post["status"] = "erro"
            post["error"] = " | ".join(f"@{c}: {m}" for c, m in erros.items())[:1000]
            failures += 1
        else:
            post["status"] = "publicado"
            post["published_at"] = now.isoformat()
            post.pop("error", None)
        pj.write_text(json.dumps(post, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if not git_save(pj, f"Status de {pj.parent.name}: {post['status']}"):
            print("  AVISO: não consegui salvar o status no repositório")
            failures += 1
        changed = True

    if not changed:
        print("Nada para publicar agora.")
    if failures:
        sys.exit(1)  # faz o GitHub te avisar por e-mail


if __name__ == "__main__":
    main()
