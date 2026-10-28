/**
 * Robô de DM do Instagram por webhook (Cloudflare Workers).
 *
 * A Meta chama este endereço a cada DM recebida. O robô:
 *   - responde só quem escreveu, na hora, com os textos aprovados (dm/respostas.json no GitHub);
 *   - escolhe o texto por regras fixas (palavras-chave), igual ao robô do GitHub;
 *   - fica quieto quando alguém da equipe responde à mão (eventos "echo");
 *   - no máximo 4 mensagens automáticas por conversa; "PARAR" encerra;
 *   - guarda só o mínimo por conversa (quais respostas já mandou e as respostas da triagem),
 *     com expiração de 30 dias. Não guarda o texto das mensagens.
 *
 * Variáveis (Cloudflare → Worker → Settings → Variables):
 *   VERIFY_TOKEN  (segredo)  texto combinado com a Meta na configuração do webhook
 *   APP_SECRET    (segredo)  "Chave secreta do app" (Configurações do app → Básico)
 *   IG_TOKEN      (segredo)  token da conta (ou TOKEN_<id da conta> para várias contas)
 * KV namespace ligado como STATE.
 */

const GRAPH = "https://graph.instagram.com/v23.0";
const RAW = "https://raw.githubusercontent.com/mikerossbr-cloud/insta-autopost/main/insta-autopost/dm";
const TTL = 60 * 60 * 24 * 30;
const ENCERRA = new Set(["qualificado", "falta_laudo", "nao_qualificado", "crise", "parar"]);

let cache = { at: 0, txt: null, cfg: null };

async function carregar() {
  if (cache.txt && Date.now() - cache.at < 5 * 60 * 1000) return cache;
  const [txt, cfg] = await Promise.all([
    fetch(`${RAW}/respostas.json`, { cf: { cacheTtl: 300 } }).then((r) => r.json()),
    fetch(`${RAW}/config.json`, { cf: { cacheTtl: 300 } }).then((r) => r.json()),
  ]);
  cache = { at: Date.now(), txt, cfg };
  return cache;
}

// ------------------------------------------------------------ palavras-chave

const norm = (s) => (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
const tem = (t, lista) => lista.some((x) => t.includes(x));

const CRISE = ["me matar", "suicid", "tirar minha vida", "tirar a minha vida", "nao aguento mais",
  "quero morrer", "vontade de morrer", "acabar com tudo", "acabar com a minha vida", "me machucar",
  "sumir de vez", "nao quero mais viver", "melhor morto"];
const OFENSA = ["golpista", "picareta", "mentiroso", "vagabundo", "safado", "pilantra", "charlatao",
  "estelionatario", "isso e golpe", "isso eh golpe", "e golpe?", "golpe isso", "isso e fraude",
  "voces sao golpe", "vcs sao golpe", "conversa fiada", "papo furado"];
const DINHEIRO = ["empresta", "emprestimo", "me manda um pix", "manda um pix", "me ajuda com dinheiro",
  "preciso de dinheiro", "me arruma um dinheiro", "pode me ajudar com um valor"];
const VALORES = ["quanto custa", "quanto cobra", "quanto voces cobram", "quanto vc cobra", "honorario",
  "porcentagem", "quanto e o servico", "valor do servico", "quanto tempo", "demora", "prazo", "cobra quanto"];
const CASAS_LEGAIS = ["betano", "bet365", "sportingbet", "superbet", "kto", "novibet", "betnacional",
  "pixbet", "esportes da sorte", "estrela bet", "estrelabet"];
const LEGAL_SIM = ["legalizada", "legalizado", "regulamentada", "regularizada", "licenciada", "com cnpj",
  "tinha cnpj", "brasileira"];
const LEGAL_NAO = ["ilegal", "sem cnpj", "estrangeir", "clandestin", "gringa", "gringo", "nao tinha cnpj",
  "nao era legal"];
const APOSTANDO = ["ainda aposto", "ainda jogo", "continuo apostando", "continuo jogando",
  "ainda estou apostando", "ainda to apostando"];
const PARAR = /^\s*(parar|pare|para|stop|sair|cancelar)\s*[.!]*\s*$/i;
const FECHO = new Set(["ok", "okay", "obrigado", "obrigada", "valeu", "vlw", "blz", "beleza", "show",
  "top", "certo", "entendi", "obg", "tmj"]);

function valores(t) {
  const out = [];
  for (const m of t.matchAll(/(\d+(?:[.,]\d+)?)\s*(milhoes|milhao|mi)\b/g)) out.push(parseFloat(m[1].replace(",", ".")) * 1e6);
  for (const m of t.matchAll(/(\d+(?:[.,]\d+)?)\s*(mil|k)\b/g)) out.push(parseFloat(m[1].replace(",", ".")) * 1e3);
  for (const m of t.matchAll(/r\$\s*(\d{1,3}(?:\.\d{3})+|\d+)(?:,\d{2})?/g)) out.push(parseFloat(m[1].replace(/\./g, "")));
  for (const m of t.matchAll(/\b(\d{1,3}(?:\.\d{3})+)(?:,\d{2})?\b/g)) out.push(parseFloat(m[1].replace(/\./g, "")));
  return out.filter((v) => v >= 1000);
}

/** Extrai informações de UMA mensagem nova e soma ao que já se sabia da conversa. */
function extrair(texto, antes) {
  const t = norm(texto);
  const info = { ...antes };
  info.crise = tem(t, CRISE);
  info.ofensa = tem(t, OFENSA);
  info.dinheiro = tem(t, DINHEIRO);
  info.valores = tem(t, VALORES);
  info.parar = PARAR.test(texto || "");
  if (tem(t, APOSTANDO)) info.apostando = true;
  const vals = valores(t);
  if (vals.length) info.perda = Math.max(...vals, info.perda || 0);
  const anos = [...t.matchAll(/\b(20[12]\d)\b/g)].map((m) => +m[1]);
  if (anos.some((a) => a >= 2024) || tem(t, ["ano passado", "esse ano", "este ano", "ate hoje", "ate agora"])) info.periodo = true;
  else if (anos.length && info.periodo == null) info.periodo = false;
  if (tem(t, LEGAL_NAO)) info.casa = false;
  else if (tem(t, LEGAL_SIM) || tem(t, CASAS_LEGAIS)) info.casa = true;
  for (const frase of t.split(/[.\n!?;]/)) {
    if (frase.includes("laudo") || frase.includes("diagnost")) info.laudo = !/\b(nao|sem|nunca|nenhum)\b/.test(frase);
  }
  for (const linha of t.split("\n")) {
    const m = linha.match(/^\s*([1-4])\s*[).:\-–]?\s*(.+)/);
    if (!m) continue;
    const resp = m[2];
    const sim = /\b(sim|s|tenho|foi|foram|isso|claro|exato)\b/.test(resp);
    const nao = /\b(nao|n|nunca|nenhum|nenhuma)\b/.test(resp);
    const v = sim === nao ? null : sim;
    if (m[1] === "1" && info.perda == null) {
      const vs = valores(/^\d+$/.test(resp.trim()) ? resp + " mil" : resp);
      if (vs.length) info.perda = Math.max(...vs);
    } else if (m[1] === "2" && v != null) info.periodo = v;
    else if (m[1] === "3" && v != null) info.casa = v;
    else if (m[1] === "4" && v != null) info.laudo = v;
  }
  const palavras = t.match(/\w+/g) || [];
  info.fechamento = palavras.length > 0 && palavras.length <= 3 && palavras.every((p) => FECHO.has(p));
  return info;
}

/** Mesmas regras do robô do GitHub (dm_bot.py → decidir). */
function decidir(info, enviados, TXT) {
  const ja = new Set(enviados);
  if (ja.has("parar")) return [null, null];
  if (info.parar) return ["parar", TXT.parar];
  if (info.crise && !ja.has("crise")) return ["crise", TXT.crise];
  if ([...ja].some((k) => ENCERRA.has(k))) return [null, null];
  if (info.dinheiro && !ja.has("pedido_dinheiro")) return ["pedido_dinheiro", TXT.pedido_dinheiro];
  if (info.ofensa) return ja.has("ofensa") ? [null, null] : ["ofensa", TXT.ofensa];

  let motivo = null;
  if (typeof info.perda === "number" && info.perda < 100000) motivo = "perda";
  else if (info.periodo === false) motivo = "periodo";
  else if (info.casa === false) motivo = "casa";
  if (motivo) return ["nao_qualificado", TXT.nao_qualificado.replace("{motivo}", TXT.nao_qualificado_motivos[motivo])];

  if (typeof info.perda === "number" && info.periodo === true && info.casa === true && info.laudo != null) {
    const k = info.laudo ? "qualificado" : "falta_laudo";
    return [k, TXT[k] + (info.apostando ? TXT.ainda_apostando_extra : "")];
  }
  if (!ja.has("triagem")) return ["triagem", TXT.triagem];
  if (info.valores && !ja.has("valores_prazo")) return ["valores_prazo", TXT.valores_prazo];
  if (info.fechamento) return [null, null];
  if (!ja.has("faltam")) {
    const faltam = [];
    if (typeof info.perda !== "number") faltam.push("perda");
    if (info.periodo == null) faltam.push("periodo");
    if (info.casa == null) faltam.push("casa");
    if (info.laudo == null) faltam.push("laudo");
    if (faltam.length) return ["faltam", TXT.faltam.replace("{itens}", faltam.map((f) => TXT.faltam_itens[f]).join("\n"))];
  }
  return [null, null];
}

// ------------------------------------------------------------ Meta

async function assinaturaValida(req, corpo, segredo) {
  const sig = req.headers.get("x-hub-signature-256") || "";
  if (!segredo || !sig.startsWith("sha256=")) return false;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(segredo),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(corpo));
  const hex = [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return hex === sig.slice(7);
}

async function enviar(token, para, texto) {
  const r = await fetch(`${GRAPH}/me/messages`, {
    method: "POST",
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: JSON.stringify({ recipient: { id: para }, message: { text: texto } }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || d.error) throw new Error(`envio falhou: ${r.status} ${JSON.stringify(d.error || d).slice(0, 200)}`);
  return d.message_id;
}

function ehTextoDoRobo(texto, TXT) {
  const t = (texto || "").replace(/\s+/g, " ").trim();
  return Object.values(TXT).some((v) => typeof v === "string" &&
    t.startsWith(v.split("{")[0].replace(/\s+/g, " ").trim().slice(0, 45)));
}

async function usernameDe(token, igsid) {
  const r = await fetch(`${GRAPH}/${igsid}?fields=username&access_token=${encodeURIComponent(token)}`);
  const d = await r.json().catch(() => ({}));
  return d.username || null;
}

async function tratar(evento, contaId, env) {
  const { txt: TXT, cfg } = await carregar();
  const wh = cfg.webhook || { modo: "desligado" };
  if (wh.modo === "desligado") return "desligado";
  const token = env[`TOKEN_${contaId}`] || env.IG_TOKEN;
  if (!token) return "sem token";

  const msg = evento.message;
  if (!msg || msg.is_deleted || msg.is_unsupported) return "ignorado";
  const outro = msg.is_echo ? evento.recipient.id : evento.sender.id;
  const chave = `conv:${contaId}:${outro}`;
  const estado = JSON.parse((await env.STATE.get(chave)) || '{"enviados":[],"mids":[],"info":{}}');

  if (msg.is_echo) {
    // mensagem enviada pela conta: se não foi o robô, é alguém da equipe → robô sai da conversa
    if (!estado.mids.includes(msg.mid) && !ehTextoDoRobo(msg.text, TXT)) {
      estado.humano = true;
      await env.STATE.put(chave, JSON.stringify(estado), { expirationTtl: TTL });
      return "equipe respondeu";
    }
    return "eco do robô";
  }
  if (await env.STATE.get(`mid:${msg.mid}`)) return "duplicado";
  await env.STATE.put(`mid:${msg.mid}`, "1", { expirationTtl: 86400 });

  if (estado.humano) return "equipe já assumiu";
  if (estado.enviados.length >= (wh.max_respostas_por_conversa || 4)) return "limite";
  if (wh.modo === "teste") {
    const u = await usernameDe(token, outro);
    if (!(wh.testadores || []).includes(u)) return "fora do teste";
  }
  const texto = msg.text || "";
  if (!texto.trim()) return "sem texto";

  estado.info = extrair(texto, estado.info);
  const [k, resposta] = decidir(estado.info, estado.enviados, TXT);
  // não guardar sinais momentâneos da mensagem
  for (const x of ["crise", "ofensa", "dinheiro", "valores", "parar", "fechamento"]) delete estado.info[x];
  if (!k) {
    await env.STATE.put(chave, JSON.stringify(estado), { expirationTtl: TTL });
    return "silêncio";
  }
  const mid = await enviar(token, outro, resposta);
  estado.enviados.push(k);
  if (mid) estado.mids.push(mid);
  await env.STATE.put(chave, JSON.stringify(estado), { expirationTtl: TTL });
  return `respondeu ${k}`;
}

export default {
  async fetch(req, env, ctx) {
    const url = new URL(req.url);
    if (req.method === "GET") {
      // verificação do webhook pela Meta
      if (url.searchParams.get("hub.mode") === "subscribe" &&
          url.searchParams.get("hub.verify_token") === env.VERIFY_TOKEN) {
        return new Response(url.searchParams.get("hub.challenge") || "", { status: 200 });
      }
      return new Response("ok", { status: 200 });
    }
    if (req.method !== "POST") return new Response("método não permitido", { status: 405 });
    const corpo = await req.text();
    if (!(await assinaturaValida(req, corpo, env.APP_SECRET))) return new Response("assinatura inválida", { status: 401 });
    let dados;
    try { dados = JSON.parse(corpo); } catch { return new Response("json inválido", { status: 400 }); }
    const tarefas = [];
    for (const entry of dados.entry || []) {
      for (const ev of entry.messaging || []) {
        tarefas.push(tratar(ev, String(entry.id), env)
          .then((r) => console.log(`[${String(ev.sender?.id || "").slice(-4)}] ${r}`))
          .catch((e) => console.log(`erro: ${String(e).slice(0, 200)}`)));
      }
    }
    ctx.waitUntil(Promise.all(tarefas));
    return new Response("EVENT_RECEIVED", { status: 200 });
  },
};

export { extrair, decidir };
export const _limparCache = () => { cache = { at: 0, txt: null, cfg: null }; };
