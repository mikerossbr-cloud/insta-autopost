"""
Robô de respostas de DM do Instagram (triagem de casos para o BPIF Advogados).

Como funciona, a cada rodada:
  1. Para cada conta de dm/config.json, lista as conversas recentes pela API oficial.
  2. Só olha conversas em que a última mensagem é da outra pessoa, dentro da janela de 24h.
  3. Fica em silêncio se alguém da equipe já respondeu à mão, se a conversa já foi encerrada
     (qualificado, não qualificado, crise) ou se o robô já mandou o máximo de mensagens.
  4. A IA (Claude) só EXTRAI informações do que a pessoa escreveu. A resposta enviada é sempre
     um dos textos aprovados de dm/respostas.json, escolhido por regras fixas neste arquivo.

Privacidade: o repositório é público. Nada das conversas é salvo nem impresso nos registros;
cada rodada relê a conversa direto do Instagram.

Variáveis de ambiente:
  IG_*                - tokens das contas (mesmos nomes de contas.json)
  ANTHROPIC_API_KEY   - chave da API da Anthropic
  DM_MODELO           - modelo (padrão claude-haiku-5-5)
  DM_DRY_RUN          - "1" para só simular (não envia nada)
"""

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
CONFIG = json.loads((BASE_DIR / "dm" / "config.json").read_text(encoding="utf-8"))
TXT = json.loads((BASE_DIR / "dm" / "respostas.json").read_text(encoding="utf-8"))
CONTAS = json.loads((BASE_DIR / "contas.json").read_text(encoding="utf-8"))

GRAPH = os.environ.get("GRAPH_URL", "https://graph.instagram.com/v23.0").rstrip("/")
ANTHROPIC_URL = os.environ.get("ANTHROPIC_URL", "https://api.anthropic.com/v1/messages")
MODELO = os.environ.get("DM_MODELO", "claude-haiku-5-5")
DRY_RUN = os.environ.get("DM_DRY_RUN") == "1"
PAUSA_ENVIO = float(os.environ.get("DM_PAUSA", "4"))
JANELA = timedelta(hours=23)  # margem dentro das 24h permitidas pela Meta

# Conversas "encerradas" para o robô: depois destes textos ele não responde mais
ENCERRA = {"qualificado", "falta_laudo", "nao_qualificado", "crise", "parar"}
PARAR = re.compile(r"^\s*(parar|pare|para|stop|sair|cancelar)\s*[.!]*\s*$", re.I)


def anon(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()[:8]


def log(msg: str):
    print(msg, flush=True)


# ---------------------------------------------------------------- Instagram

class IG:
    def __init__(self, token: str):
        self.token = token

    def get(self, path, **params):
        params["access_token"] = self.token
        r = requests.get(f"{GRAPH}/{path}", params=params, timeout=60)
        data = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in data:
            raise RuntimeError(f"Erro da API ({r.status_code}): {data.get('error', data)}")
        return data

    def send(self, recipient_id: str, text: str):
        r = requests.post(f"{GRAPH}/me/messages",
                          headers={"Authorization": f"Bearer {self.token}"},
                          json={"recipient": {"id": recipient_id}, "message": {"text": text}},
                          timeout=60)
        data = r.json() if r.content else {}
        if r.status_code >= 400 or "error" in data:
            raise RuntimeError(f"Erro ao enviar ({r.status_code}): {data.get('error', data)}")
        return data


def parse_time(s: str) -> datetime:
    return datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")


def mensagens_da_conversa(ig: IG, conv_id: str) -> list:
    data = ig.get(conv_id, fields="messages.limit(20){id,created_time,from,to,message}")
    msgs = data.get("messages", {}).get("data", [])
    completas = []
    for m in msgs:
        if "created_time" not in m or "from" not in m:
            m = ig.get(m["id"], fields="id,created_time,from,to,message")
        completas.append(m)
    completas.sort(key=lambda m: parse_time(m["created_time"]))
    return completas


# ---------------------------------------------------------------- textos

def prefixo(chave: str) -> str:
    base = TXT[chave].split("{")[0]
    return re.sub(r"\s+", " ", base).strip()[:45]


PREFIXOS = {k: prefixo(k) for k in
            ["triagem", "faltam", "qualificado", "falta_laudo", "nao_qualificado",
             "valores_prazo", "crise", "ofensa", "pedido_dinheiro", "parar"]}


def identificar_texto(texto: str):
    """Diz qual resposta aprovada é esta mensagem nossa (ou None se foi escrita à mão)."""
    t = re.sub(r"\s+", " ", texto or "").strip()
    for chave, p in PREFIXOS.items():
        if t.startswith(p):
            return chave
    return None


# ---------------------------------------------------------------- IA (só extração)

PROMPT = """Você analisa mensagens de Direct do Instagram enviadas por pessoas que perderam dinheiro em casas de aposta (bets) e procuram um escritório de advocacia que entra com ação para recuperar o valor.
Leia SOMENTE as mensagens da pessoa e extraia as informações. Não escreva resposta para a pessoa.
Responda apenas com um JSON válido, sem texto antes ou depois, com estas chaves:
{
 "crise": true se a pessoa demonstra desespero grave, fala em se matar, sumir, não aguentar mais viver ou em se machucar,
 "ofensa_ou_golpe": true se a pessoa ofende ou acusa de golpe/fraude,
 "pedido_dinheiro": true se a pessoa pede dinheiro emprestado ou ajuda financeira direta,
 "pergunta_valores_prazo": true se pergunta quanto custa, honorários, porcentagem ou quanto tempo demora,
 "perda_liquida_reais": número aproximado da perda líquida em reais (perdeu menos o que sacou) ou null se não informou; "150 mil" = 150000; se só informou quanto depositou, use esse valor,
 "desde_2024": true se as perdas são de 2024 em diante (ou "ano passado"/"este ano" considerando que hoje é {hoje}), false se foram só antes de 2024, null se não informou,
 "casa_legalizada": true se a pessoa disse que a casa era legalizada/regulamentada/com CNPJ no Brasil, ou citou uma marca grande e conhecida que operava legalmente no Brasil; false se disse que era site ilegal, estrangeiro, clandestino ou sem CNPJ; null se não informou ou se você não tem certeza,
 "tem_laudo": true se tem laudo/diagnóstico de ludopatia, false se disse que não tem, null se não informou,
 "ainda_apostando": true se diz que ainda aposta,
 "fora_do_assunto": true se a mensagem não tem relação com perdas em apostas nem com o escritório (ex.: só "ok", "obrigado", emoji, outro assunto)
}"""


# ---------------------------------------------------------------- palavras-chave (sem IA)

def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _tem(t: str, termos) -> bool:
    return any(x in t for x in termos)


CRISE = ["me matar", "suicid", "tirar minha vida", "tirar a minha vida", "nao aguento mais",
         "quero morrer", "vontade de morrer", "acabar com tudo", "acabar com a minha vida",
         "me machucar", "sumir de vez", "nao quero mais viver", "melhor morto"]
OFENSA = ["golpista", "picareta", "mentiroso", "vagabundo", "safado", "pilantra", "charlatao",
          "estelionatario", "isso e golpe", "isso eh golpe", "e golpe?", "golpe isso", "isso e fraude",
          "voces sao golpe", "vcs sao golpe", "conversa fiada", "papo furado"]
DINHEIRO = ["empresta", "emprestimo", "me manda um pix", "manda um pix", "me ajuda com dinheiro",
            "preciso de dinheiro", "me arruma um dinheiro", "pode me ajudar com um valor"]
VALORES = ["quanto custa", "quanto cobra", "quanto voces cobram", "quanto vc cobra", "honorario",
           "porcentagem", "quanto e o servico", "valor do servico", "quanto tempo", "demora",
           "prazo", "cobra quanto"]
CASAS_LEGAIS = ["betano", "bet365", "sportingbet", "superbet", "kto", "novibet", "betnacional",
                "pixbet", "esportes da sorte", "estrela bet", "estrelabet"]
LEGAL_SIM = ["legalizada", "legalizado", "regulamentada", "regularizada", "licenciada", "com cnpj",
             "tinha cnpj", "brasileira"]
LEGAL_NAO = ["ilegal", "sem cnpj", "estrangeir", "clandestin", "gringa", "gringo", "nao tinha cnpj",
             "nao era legal"]
APOSTANDO = ["ainda aposto", "ainda jogo", "continuo apostando", "continuo jogando",
             "ainda estou apostando", "ainda to apostando", "ainda tô apostando"]
SIM = re.compile(r"\b(sim|s|tenho|foi|foram|isso|claro|exato|yes)\b")
NAO = re.compile(r"\b(nao|n|nunca|nenhum|nenhuma)\b")


def _valores(t: str) -> list:
    vals = []
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(milhoes|milhao|mi)\b", t):
        vals.append(float(m.group(1).replace(",", ".")) * 1_000_000)
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(mil|k)\b", t):
        vals.append(float(m.group(1).replace(",", ".")) * 1_000)
    for m in re.finditer(r"r\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{2})?", t):
        vals.append(float(m.group(1).replace(".", "")))
    for m in re.finditer(r"\b(\d{1,3}(?:\.\d{3})+)(?:,\d{2})?\b", t):
        vals.append(float(m.group(1).replace(".", "")))
    return [v for v in vals if v >= 1000]


def extrair_palavras(textos_pessoa: list) -> dict:
    t = _norm(" \n ".join(textos_pessoa))
    info = {"crise": _tem(t, CRISE), "ofensa_ou_golpe": _tem(t, OFENSA),
            "pedido_dinheiro": _tem(t, DINHEIRO), "pergunta_valores_prazo": _tem(t, VALORES),
            "perda_liquida_reais": None, "desde_2024": None, "casa_legalizada": None,
            "tem_laudo": None, "ainda_apostando": _tem(t, APOSTANDO), "fora_do_assunto": False}
    vals = _valores(t)
    if vals:
        info["perda_liquida_reais"] = max(vals)
    anos = [int(a) for a in re.findall(r"\b(20[12]\d)\b", t)]
    if any(a >= 2024 for a in anos) or _tem(t, ["ano passado", "esse ano", "este ano", "ate hoje",
                                                  "ate agora", "ate o fechamento", "ate fechar"]):
        info["desde_2024"] = True
    elif anos:
        info["desde_2024"] = False
    if _tem(t, LEGAL_NAO):
        info["casa_legalizada"] = False
    elif _tem(t, LEGAL_SIM) or _tem(t, CASAS_LEGAIS):
        info["casa_legalizada"] = True
    for frase in re.split(r"[.\n!?;]", t):
        if "laudo" in frase or "diagnost" in frase:
            info["tem_laudo"] = not bool(re.search(r"\b(nao|sem|nunca|nenhum)\b", frase))
    # respostas numeradas ao questionário: "1. 150 mil / 2. sim / 3. sim / 4. nao"
    for linha in t.splitlines():
        m = re.match(r"\s*([1-4])\s*[).:\-–]?\s*(.+)", linha)
        if not m:
            continue
        n, resp = m.group(1), m.group(2)
        sim, nao = bool(SIM.search(resp)), bool(NAO.search(resp))
        valor = None if sim == nao else sim
        if n == "1" and info["perda_liquida_reais"] is None:
            v = _valores(resp + " mil" if re.fullmatch(r"\d+", resp.strip()) else resp)
            info["perda_liquida_reais"] = max(v) if v else None
        elif n == "2" and valor is not None and info["desde_2024"] is None:
            info["desde_2024"] = valor
        elif n == "3" and valor is not None and info["casa_legalizada"] is None:
            info["casa_legalizada"] = valor
        elif n == "4" and valor is not None and info["tem_laudo"] is None:
            info["tem_laudo"] = valor
    ultima = _norm(textos_pessoa[-1] if textos_pessoa else "")
    palavras = re.findall(r"\w+", ultima)
    if len(palavras) <= 3 and all(p in {"ok", "okay", "obrigado", "obrigada", "valeu", "vlw", "blz",
                                        "beleza", "show", "top", "certo", "entendi", "obg", "tmj"}
                                  for p in palavras):
        info["fora_do_assunto"] = True
    return info


def extrair(textos_pessoa: list) -> dict:
    chave = os.environ.get("ANTHROPIC_API_KEY", "")
    if not chave:
        return extrair_palavras(textos_pessoa)  # sem chave: modo palavras-chave
    hoje = datetime.now(timezone(timedelta(hours=-3))).strftime("%d/%m/%Y")
    conteudo = "\n".join(f"- {t}" for t in textos_pessoa if t)
    r = requests.post(ANTHROPIC_URL, timeout=60, headers={
        "x-api-key": chave, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": MODELO, "max_tokens": 400,
              "system": PROMPT.replace("{hoje}", hoje),
              "messages": [{"role": "user", "content": f"Mensagens da pessoa:\n{conteudo}"}]})
    data = r.json()
    if r.status_code >= 400:
        raise RuntimeError(f"Erro da IA ({r.status_code}): {data.get('error', data)}")
    texto = "".join(b.get("text", "") for b in data.get("content", []))
    m = re.search(r"\{.*\}", texto, re.S)
    if not m:
        raise RuntimeError("IA não devolveu JSON")
    return json.loads(m.group(0))


# ---------------------------------------------------------------- decisão (regras fixas)

def decidir(info: dict, enviados: list):
    """Devolve (chave_da_resposta, texto) ou (None, None) para ficar em silêncio."""
    ja = set(enviados)
    if "parar" in ja:
        return None, None  # a pessoa pediu para parar: nunca mais responde
    if info.get("parar"):
        return "parar", TXT["parar"]
    if info.get("crise") and "crise" not in ja:
        return "crise", TXT["crise"]  # crise vale mesmo em conversa já encerrada
    if ja & ENCERRA:
        return None, None
    if info.get("pedido_dinheiro") and "pedido_dinheiro" not in ja:
        return "pedido_dinheiro", TXT["pedido_dinheiro"]
    if info.get("ofensa_ou_golpe"):
        return ("ofensa", TXT["ofensa"]) if "ofensa" not in ja else (None, None)

    perda = info.get("perda_liquida_reais")
    periodo, casa, laudo = info.get("desde_2024"), info.get("casa_legalizada"), info.get("tem_laudo")

    motivo = None
    if isinstance(perda, (int, float)) and perda < 100_000:
        motivo = "perda"
    elif periodo is False:
        motivo = "periodo"
    elif casa is False:
        motivo = "casa"
    if motivo:
        return "nao_qualificado", TXT["nao_qualificado"].replace(
            "{motivo}", TXT["nao_qualificado_motivos"][motivo])

    if isinstance(perda, (int, float)) and periodo is True and casa is True and laudo is not None:
        chave = "qualificado" if laudo else "falta_laudo"
        texto = TXT[chave]
        if info.get("ainda_apostando"):
            texto += TXT["ainda_apostando_extra"]
        return chave, texto

    if "triagem" not in ja:
        return "triagem", TXT["triagem"]
    if info.get("pergunta_valores_prazo") and "valores_prazo" not in ja:
        return "valores_prazo", TXT["valores_prazo"]
    if info.get("fora_do_assunto"):
        return None, None
    if "faltam" not in ja:
        faltam = []
        if not isinstance(perda, (int, float)):
            faltam.append("perda")
        if periodo is None:
            faltam.append("periodo")
        if casa is None:
            faltam.append("casa")
        if laudo is None:
            faltam.append("laudo")
        if faltam:
            itens = "\n".join(TXT["faltam_itens"][f] for f in faltam)
            return "faltam", TXT["faltam"].replace("{itens}", itens)
    return None, None  # já perguntou tudo uma vez: a equipe assume


# ---------------------------------------------------------------- rodada

def processar_conta(nome: str, token: str, stats: dict):
    ig = IG(token)
    eu = ig.get("me", fields="user_id,username")
    meus_ids = {str(eu.get("user_id", "")), str(eu.get("id", ""))} - {""}
    meu_user = eu.get("username", nome)
    agora = datetime.now(timezone.utc)
    convs = ig.get("me/conversations", platform="instagram", fields="id,updated_time", limit=25)
    lista = convs.get("data", [])
    log(f"  conta conectada: id={'sim' if meus_ids else 'nao'}; conversas visíveis pela API: {len(lista)}")
    stats["conversas_vistas"] = stats.get("conversas_vistas", 0) + len(lista)
    envios = 0
    for c in lista:
        if envios >= CONFIG["max_envios_por_rodada"]:
            break
        if "updated_time" in c and agora - parse_time(c["updated_time"]) > JANELA:
            stats["fora_24h"] = stats.get("fora_24h", 0) + 1
            continue
        cid = anon(c["id"])
        try:
            msgs = mensagens_da_conversa(ig, c["id"])
        except Exception as e:
            log(f"  [{cid}] erro lendo conversa: {str(e)[:150]}")
            stats["erros"] += 1
            continue
        if not msgs:
            continue

        def e_meu(m):
            f = m.get("from", {})
            return str(f.get("id")) in meus_ids or f.get("username") == meu_user

        ultima = msgs[-1]
        if e_meu(ultima):
            stats["ja_respondidas"] = stats.get("ja_respondidas", 0) + 1
            continue  # já respondido
        if agora - parse_time(ultima["created_time"]) > JANELA:
            continue  # fora da janela de 24h
        pessoa = ultima.get("from", {})
        if CONFIG["modo"] == "desligado":
            continue
        if CONFIG["modo"] == "teste" and pessoa.get("username") not in CONFIG["testadores"]:
            continue

        nossos = [m for m in msgs if e_meu(m)]
        enviados = [identificar_texto(m.get("message", "")) for m in nossos]
        if any(k is None for k in enviados):
            log(f"  [{cid}] equipe já respondeu à mão: robô fica quieto")
            stats["equipe"] += 1
            continue
        if len(enviados) >= CONFIG["max_respostas_por_conversa"]:
            continue

        # mensagens da pessoa desde a nossa última resposta (ou todas, se nunca respondemos)
        ult_nossa = parse_time(nossos[-1]["created_time"]) if nossos else None
        textos = [m.get("message", "") for m in msgs if not e_meu(m)
                  and (ult_nossa is None or parse_time(m["created_time"]) > ult_nossa)]
        todas_da_pessoa = [m.get("message", "") for m in msgs if not e_meu(m)]
        try:
            info = extrair(todas_da_pessoa[-12:])
            info["parar"] = any(PARAR.match(x or "") for x in textos)
        except Exception as e:
            log(f"  [{cid}] erro na IA: {str(e)[:150]}")
            stats["erros"] += 1
            continue
        if not any(t.strip() for t in textos):
            continue  # só mídia/figurinha: a equipe vê
        chave, texto = decidir(info, enviados)
        if not chave:
            stats["silencio"] += 1
            continue
        if DRY_RUN:
            log(f"  [{cid}] simulação: responderia '{chave}'")
        else:
            try:
                ig.send(pessoa["id"], texto)
                envios += 1
                time.sleep(PAUSA_ENVIO)
            except Exception as e:
                log(f"  [{cid}] erro ao enviar: {str(e)[:150]}")
                stats["erros"] += 1
                continue
            log(f"  [{cid}] respondeu '{chave}'")
        stats["respostas"] += 1
        stats[chave] = stats.get(chave, 0) + 1


def main():
    stats = {"respostas": 0, "silencio": 0, "equipe": 0, "erros": 0}
    if CONFIG["modo"] == "desligado":
        log("Robô de DM desligado (dm/config.json).")
        return
    for nome in CONFIG["contas"]:
        cfg = CONTAS.get(nome)
        token = os.environ.get(cfg["secret"], "") if cfg else ""
        if not token:
            log(f"@{nome}: sem token, pulando")
            stats["erros"] += 1
            continue
        log(f"@{nome}:")
        try:
            processar_conta(nome, token, stats)
        except Exception as e:
            log(f"  erro: {str(e)[:200]}")
            stats["erros"] += 1
    log(f"Resumo: {json.dumps(stats, ensure_ascii=False)}")
    if stats["erros"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
