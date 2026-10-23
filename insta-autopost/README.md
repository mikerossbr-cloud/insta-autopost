# Robô de postagem automática no Instagram

Publica sozinho na sua conta do Instagram, no horário marcado, usando a API oficial da Meta.
Roda de graça no GitHub (Actions + Pages). Sem servidor, sem mensalidade.

## Como funciona

- Cada post é uma pasta dentro de `posts/`, com as mídias e um `post.json`.
- A cada 15 minutos o GitHub roda o robô (`publish.py`). Se um post está `agendado` e a hora chegou, ele publica.
- Depois de publicar, o robô muda o status para `publicado` (ou `erro`, com o motivo) e salva no repositório.
- Toda segunda o token do Instagram é renovado automaticamente (ele venceria em 60 dias).

## Formato de um post

Pasta: `posts/2026-11-03_1830-nome-do-post/`

```json
{
  "scheduled_at": "2026-11-03T18:30:00-03:00",
  "type": "auto",
  "media": ["capa.jpg", "slide2.jpg", "slide3.jpg"],
  "caption": "Legenda do post\n\n#hashtag",
  "status": "agendado"
}
```

- `scheduled_at`: data e hora com fuso (`-03:00` = Brasília).
- `type`: `auto` decide sozinho (1 imagem = foto, 1 vídeo = reels, vários arquivos = carrossel). Também aceita `image`, `reel`, `carousel`.
- `media`: arquivos na mesma pasta, na ordem em que aparecem. Imagens em JPG. Carrossel de 2 a 10 itens.
- `status`:
  - `rascunho`: o robô ignora.
  - `agendado`: será publicado na hora marcada.
  - `publicado` / `erro`: preenchidos pelo robô.

Para tentar de novo um post com erro, corrija o problema e volte o status para `agendado`.

## Várias contas

As contas ficam em `contas.json`. Cada uma aponta para o segredo do GitHub com o token dela:

```json
{
  "mikerossbr3": {"secret": "IG_ACCESS_TOKEN", "padrao": true},
  "outraconta":  {"secret": "IG_TOKEN_OUTRACONTA", "padrao": true}
}
```

- No `post.json`, `"contas": ["mikerossbr3", "outraconta"]` escolhe onde publicar.
- Sem esse campo, o post vai para todas as contas com `"padrao": true`.
- O resultado de cada conta fica em `resultado`. Se uma falhar, o post fica com status `erro`;
  ao voltar para `agendado`, só as contas que falharam são tentadas de novo (sem post duplicado).
- Para adicionar uma conta: torná-la Testadora do Instagram no app da Meta, gerar o token dela,
  criar o segredo `IG_TOKEN_<NOME>` no GitHub e incluir a conta em `contas.json`.

## Configuração (uma vez só)

1. **Repositório público** no GitHub com estes arquivos. Ele precisa ser público para o Instagram conseguir baixar as imagens, e assim o GitHub Actions também fica ilimitado e grátis.
2. **GitHub Pages**: em Settings → Pages, escolha "Deploy from a branch", branch `main`, pasta `/ (root)`.
3. **Segredos** em Settings → Secrets and variables → Actions → New repository secret:
   - `IG_ACCESS_TOKEN`: o token gerado no painel da Meta.
   - `GH_PAT`: um token pessoal do GitHub (fine-grained) só para este repositório, com permissão **Secrets: Read and write**. Ele é usado para renovar o token do Instagram.
4. Na aba **Actions**, ative os workflows, se o GitHub pedir.

## Avisos

- Por ser repositório público, as mídias e legendas dos posts agendados ficam visíveis no GitHub antes da hora. Não coloque nada confidencial.
- O GitHub pode atrasar a execução agendada em alguns minutos, principalmente em horários de pico.
- Se algo der errado, o GitHub manda e-mail avisando que o workflow falhou.
