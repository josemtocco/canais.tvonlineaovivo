# TV Online Ao Vivo → Lista M3U para SS IPTV

Projeto que gera automaticamente uma lista **M3U** com todos os canais do site
**https://canais.tvonlineaovivo.com.br/**, otimizada para tocar no **SS IPTV**
(também funciona no VLC e em qualquer player IPTV).

A lista se atualiza sozinha **a cada 6 horas** pelo GitHub Actions.

---

## O que o projeto faz em cada atualização

| Etapa | Ação |
|-------|------|
| 1 | Lê **todas as 37 categorias** do site (sitemap oficial) |
| 2 | Entra em cada categoria e pega a 1ª página |
| 3 | Clica no botão **“Carregar mais”** repetidamente até não existir mais nada |
| 4 | Abre cada página de canal e extrai o link **direto do vídeo (HLS)** |
| 5 | **Testa se está tocando** (baixa o manifesto e confirma que tem segmento vivo) |
| 6 | **Reconcilia**: mantém ativos, remove inativos só na **6ª falha seguida**, adiciona novos |
| 7 | Gera `tvonlineaovivo.m3u` com nome, categoria e logo |

---

## Reconciliação (regra das 6 atualizações)

O arquivo `estado_canais.json` guarda um contador de falhas por canal.

- Canal **tocando** → volta para `falhas = 0` e continua na lista.
- Canal **que falhou** → soma 1 falha e **continua na lista**.
- Canal que chegou a **6 falhas seguidas** → é **removido**.
- Canal **novo** que está tocando → é **adicionado** na hora.
- Se o **site inteiro** cair/for bloqueado → a lista atual é **preservada**
  (não conta falha em massa, para não zerar tudo de uma vez).

Como são 4 execuções por dia, 6 falhas ≈ 1 dia e meio de problema antes de remover.
Para mudar, edite no topo de `gerar_m3u.py`:

```python
LIMITE_FALHAS = 6
```

---

## Otimizações para o SS IPTV

Cada canal é escrito assim:

```
#EXTINF:-1 tvg-id="Record" tvg-name="Record" group-title="Geral Brasil" content-type="video",Record
https://.../playlist.m3u8
```

- `content-type="video"` → o SS IPTV trata como **canal ao vivo** (não como filme).
- `group-title` → cria as **pastas/categorias** iguais às do site.
- `tvg-name` / `tvg-id` → nome bonito e organizado nas telas.
- Links conferidos **sem precisar de Referer** — o SS IPTV toca direto.
- Canais que só existem em **YouTube/iframe** são ignorados (não tocam em player IPTV).

> Observação: o site não fornece logo próprio para os canais (usa uma imagem
genérica do SBT em todos). Por isso o campo `tvg-logo` sai vazio e o SS IPTV
mostra o **nome do canal** — o que fica mais correto do que mostrar logo errado.

---

## Como usar a lista

### No SS IPTV (TV Samsung / LG / Android TV)

1. Publique este projeto no GitHub (veja abaixo) e aguarde a 1ª execução.
2. Abra o SS IPTV → **Configurações → Lista de reprodução → Adicionar**.
3. Escolha **URL** e cole o link **raw** do seu arquivo:

```
https://raw.githubusercontent.com/SEU-USUARIO/SEU-REPO/main/tvonlineaovivo.m3u
```

4. Ou baixe o arquivo `tvonlineaovivo.m3u` e use **Arquivo local**.

### No VLC

Mídia → Abrir Arquivo → escolha `tvonlineaovivo.m3u`.

---

## Como publicar no GitHub (passo a passo)

1. Crie um repositório **privado ou público** (ex.: `tvonlineaovivo-m3u`).
2. Envie **todos os arquivos** deste projeto, mantendo a pasta
   `.github/workflows/atualizar.yml`.
3. Vá em **Settings → Actions → General → Workflow permissions**
   e marque **Read and write permissions** → Save.
4. Vá em **Actions → Atualizar lista M3U → Run workflow** para rodar a 1ª vez.
5. Depois disso roda sozinho **de 6 em 6 horas**.

---

## Rodar no seu PC (Windows)

1. Instale o Python de https://www.python.org/downloads/
   (marque **“Add python.exe to PATH”**).
2. Dê **duplo clique** em `rodar_no_brasil.bat`.
3. Ao final aparece o arquivo `tvonlineaovivo.m3u` na pasta.

Rodar do Brasil é melhor: canais com bloqueio regional (403) tocam aqui e
não tocam nos servidores dos EUA do GitHub Actions.

---

## Arquivos gerados

| Arquivo | Para que serve |
|---------|----------------|
| `tvonlineaovivo.m3u` | **A lista** para colocar no SS IPTV / VLC |
| `tvonlineaovivo.xml` | EPG simples (mostra “Ao Vivo” na grade) |
| `estado_canais.json` | Memória do contador de falhas (não apague) |
| `relatorio.json` | Resumo da última execução |
| `ultima_execucao.log` | Log completo do que aconteceu |

---

## Configurações rápidas (variáveis)

No topo de `gerar_m3u.py`:

```python
LIMITE_FALHAS = 6            # remover após N falhas
INCLUIR_INTERNACIONAIS = 1   # 0 = só Brasil
LOG_DETALHADO = 1            # 0 = log mais curto
```

---

## Resultado do teste real

Execução de validação completa:

- **124** canais únicos encontrados no site
- **73** canais tocando e incluídos na lista
- **43** sem sinal no momento (contam falha, saem só na 6ª)
- **7** só em YouTube/iframe (não tocam em IPTV, ignorados)
- Tempo total: ~4 minutos

Muitos dos 43 “sem sinal” têm **bloqueio por região (HTTP 403)** e voltam a
tocar quando a execução é feita a partir do Brasil.

---

## Aviso

Este projeto apenas organiza os links **que o próprio site publica** em uma
lista M3U. Os canais pertencem aos seus donos; a transmissão é de
responsabilidade da fonte.
