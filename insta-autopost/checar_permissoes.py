"""Testa o que o token de uma conta consegue fazer na API do Instagram, sem imprimir nenhum conteúdo.
Uso: CONTA=davismkt python insta-autopost/checar_permissoes.py   (lê o token de IG_TOKEN_<CONTA>)"""
import json, os, sys, requests
G = "https://graph.instagram.com/v23.0"
conta = os.environ.get("CONTA", "davismkt")
tok = os.environ.get("IG_TOKEN_" + conta.upper())
if not tok: sys.exit(f"segredo IG_TOKEN_{conta.upper()} vazio")
def get(path, **p):
    r = requests.get(f"{G}/{path}", params={**p, "access_token": tok}, timeout=30)
    try: j = r.json()
    except Exception: j = {}
    return r.status_code, j
def teste(nome, st, j):
    if st == 200: print(f"OK    {nome}")
    else:
        e = j.get("error", {})
        print(f"FALHA {nome}: HTTP {st} · código {e.get('code')} · {e.get('message', '')[:160]}")
    return st == 200
st, me = get("me", fields="user_id,username,account_type")
teste("token válido / perfil", st, me)
if st == 200: print(f"      conta: @{me.get('username')} ({me.get('account_type')})")
teste("ler conversas (mensagens)", *get("me/conversations", platform="instagram", limit=1))
st, med = get("me/media", limit=5, fields="id,comments_count")
teste("listar posts", st, med)
mid = next((m["id"] for m in med.get("data", []) if m.get("comments_count")), None) if st == 200 else None
if mid: teste("ler comentários de um post", *get(f"{mid}/comments", limit=1, fields="id"))
else: print("--    ler comentários: nenhum post recente com comentário para testar")
