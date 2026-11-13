#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TV Online Ao Vivo  ->  Lista M3U otimizada para SS IPTV
========================================================

Fonte: https://canais.tvonlineaovivo.com.br/

O que este script faz em cada execucao:

 1. Descobre TODAS as categorias do site (sitemap oficial do WordPress).
 2. Em cada categoria, coleta todos os canais da 1a pagina.
 3. Depois clica no botao "Carregar mais" (paginacao AJAX do tema)
    repetidamente ate nao existir mais nenhuma pagina/conteudo.
 4. Visita cada pagina de canal e extrai o link DIRETO do stream (HLS .m3u8).
 5. Testa cada link: so entra na lista o canal que esta realmente tocando.
 6. Reconciliacao:
      - canal ativo                    -> mantem
      - canal que falhou                -> +1 falha, remove so na 6a falha seguida
      - canal novo que esta tocando     -> adiciona
 7. Gera a lista M3U com nome do canal, logo, categoria (group-title) e
    os atributos que fazem o SS IPTV reconhecer canal ao vivo de verdade.

Sem dependencias externas: somente a biblioteca padrao do Python 3.8+.
"""

import gzip
import html
import io
import json
import os
import re
import socket
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# CONFIGURACOES
# ---------------------------------------------------------------------------

BASE = "https://canais.tvonlineaovivo.com.br"

ARQ_LISTA = "tvonlineaovivo.m3u"
ARQ_ESTADO = "estado_canais.json"
ARQ_RELATORIO = "relatorio.json"
ARQ_LOG = "ultima_execucao.log"

# Quantas atualizacoes seguidas um canal pode falhar antes de ser removido.
LIMITE_FALHAS = 6

# Timeout (segundos) das requisicoes.
TIMEOUT_PAGINA = 25
TIMEOUT_STREAM = 15

# Threads paralelas.
THREADS_CATALOGO = 8
THREADS_TESTE = 12

# Numero maximo de paginas "Carregar mais" por categoria (trava de seguranca).
MAX_PAGINAS = 120

# Incluir canais de fora do Brasil (Venezuela, El Salvador, Angola, Etiopia...).
INCLUIR_INTERNACIONAIS = os.environ.get("INCLUIR_INTERNACIONAIS", "1") != "0"

# Mostrar no log cada canal testado.
LOG_DETALHADO = os.environ.get("LOG_DETALHADO", "1") != "0"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")

DIR_SCRIPT = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# UTILIDADES
# ---------------------------------------------------------------------------

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def log(msg):
    print(msg, flush=True)


def baixar(url, timeout=TIMEOUT_PAGINA, extra_headers=None, binario=False):
    """Baixa uma URL tratando gzip e devolvendo texto (ou bytes)."""
    headers = {
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, identity",
        "Connection": "close",
    }
    if extra_headers:
        headers.update(extra_headers)

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
        dados = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            try:
                dados = gzip.decompress(dados)
            except OSError:
                pass
        if binario:
            return dados
        for enc in ("utf-8", "latin-1"):
            try:
                return dados.decode(enc)
            except UnicodeDecodeError:
                continue
        return dados.decode("utf-8", "ignore")


def titulo_limpo(txt):
    txt = html.unescape(txt or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt


def normalizar(txt):
    """Chave de comparacao: sem acento, minusculo, sem pontuacao."""
    txt = unicodedata.normalize("NFKD", txt or "")
    txt = "".join(c for c in txt if not unicodedata.combining(c))
    txt = txt.lower()
    txt = re.sub(r"[^a-z0-9]+", "", txt)
    return txt


def bonito(slug):
    """'brazil-noticias' -> 'Brazil Noticias' ; 'series' -> 'Series'."""
    palavras = re.split(r"[-_/]+", slug)
    pequenas = {"de", "da", "do", "das", "dos", "e"}
    out = []
    for i, p in enumerate(palavras):
        if not p:
            continue
        p = p.capitalize()
        if i > 0 and p.lower() in pequenas:
            p = p.lower()
        out.append(p)
    return " ".join(out)


def nome_categoria_amigavel(slug, mapa):
    """Traduz o slug da categoria para um titulo bonito em portugues."""
    partes = [p for p in slug.split("/") if p]
    if not partes:
        return "Outros"

    base_slug = partes[0]
    nome_base = mapa.get(base_slug) or bonito(base_slug).title()

    if len(partes) == 1:
        return nome_base

    # 2a parte: pais ou subcategoria
    sub = partes[1]
    pais = ""
    resto = sub
    for pref, nome_pais in (
        ("brazil", "Brasil"), ("venezuela", "Venezuela"),
        ("el-salvador", "El Salvador"), ("angola", "Angola"),
        ("ethiopia", "Etiópia"),
    ):
        if sub == pref or sub.startswith(pref + "-"):
            pais = nome_pais
            resto = sub[len(pref):].lstrip("-")
            break

    # remove repeticao: 'religioso-el-salvador-religioso' -> resto 'religioso'
    if resto:
        resto_norm = normalizar(resto)
        if resto_norm in (normalizar(p) for p in partes[:1]) or resto_norm.startswith(normalizar(base_slug)):
            resto = ""

    if pais and not resto:
        return f"{nome_base} {pais}"
    if pais and resto:
        return f"{nome_base} {pais} — {(mapa.get(resto) or bonito(resto).title())}"
    return f"{nome_base} — {bonito(resto).title()}"


# ---------------------------------------------------------------------------
# 1) DESCOBERTA DE CATEGORIAS
# ---------------------------------------------------------------------------

CATEGORIAS_PT = {
    "animacao": "Animação",
    "infantil": "Infantil",
    "classicos": "Clássicos",
    "series": "Séries",
    "cultura": "Cultura",
    "noticias": "Notícias",
    "educacao": "Educação",
    "entretenimento": "Entretenimento",
    "familia": "Família",
    "filmes": "Filmes",
    "geral": "Geral",
    "legislativo": "Legislativo",
    "musica": "Música",
    "religioso": "Religioso",
    "esportes": "Esportes",
    "estilo-de-vida": "Estilo de Vida",
}

def listar_categorias():
    """Le o sitemap oficial de categorias do site."""
    url = BASE + "/wp-sitemap-taxonomies-vidmov_video_category-1.xml"
    texto = baixar(url)
    urls = re.findall(r"<loc>([^<]+)</loc>", texto)
    slugs = []
    for u in urls:
        m = re.search(r"/video-genero/(.+?)/?$", u.strip())
        if m:
            slugs.append(m.group(1).strip("/"))

    # Remove duplicatas preservando ordem
    vistos, unicos = set(), []
    for s in slugs:
        if s and s not in vistos:
            vistos.add(s)
            unicos.append(s)

    if not unicos:
        # Fallback: menu principal da home
        home = baixar(BASE + "/")
        for s in re.findall(r'href="' + re.escape(BASE) + r'/video-genero/([^"?]+)/"', home):
            s = s.strip("/")
            if s and s not in vistos:
                vistos.add(s)
                unicos.append(s)

    log(f"Categorias encontradas no site: {len(unicos)}")
    return unicos


# ---------------------------------------------------------------------------
# 2) COLETA DE CANAIS (1a pagina + botao "Carregar mais")
# ---------------------------------------------------------------------------

RE_ITEM = re.compile(
    r'<h3 class="entry-title[^"]*">\s*<a[^>]*href="([^"]+)"[^>]*title="([^"]*)"', re.S)


def extrair_itens(trecho_html):
    itens = []
    for url, titulo in RE_ITEM.findall(trecho_html):
        url = url.split("#")[0]
        titulo = titulo_limpo(titulo)
        if "/canal/" not in url:
            continue
        if not titulo:
            continue
        itens.append((url, titulo))
    if not itens:
        # fallback mais tolerante
        for m in re.finditer(r'<a class="post-listing-title" href="([^"]+)"[^>]*>(.*?)</a>', trecho_html, re.S):
            url = m.group(1).split("#")[0]
            titulo = titulo_limpo(re.sub(r"<[^>]+>", "", m.group(2)))
            if "/canal/" in url and titulo:
                itens.append((url, titulo))
    return itens


def ler_config_ajax(pagina_html):
    """Extrai o objeto JS do tema com a URL do admin-ajax e o token de seguranca."""
    dec = json.JSONDecoder()
    marca = "var vidmov_jav_js_object = "
    i = pagina_html.find(marca)
    if i < 0:
        return None
    try:
        obj, _ = dec.raw_decode(pagina_html[i + len(marca):])
        return obj
    except ValueError:
        return None


def clicar_carregar_mais(cfg, categoria_url, pagina):
    """Simula o clique no botao 'Carregar mais' (paginacao AJAX do tema)."""
    if not cfg:
        return ""
    corpo = [
        ("action", "beeteam368_loadmore_posts"),
        ("template", "template-parts/archive/item"),
        ("style", "alyssa"),
        ("paged", str(pagina)),
        ("security", cfg.get("security", "")),
    ]
    qv = dict(cfg.get("query_vars") or {})
    qv["paged"] = pagina
    qv["nopaging"] = False

    def empilhar(prefixo, dicionario):
        for k, v in dicionario.items():
            chave = f"{prefixo}[{k}]"
            if isinstance(v, list):
                if not v:
                    corpo.append((chave + "[]", ""))
                for item in v:
                    corpo.append((chave + "[]", str(item)))
            elif isinstance(v, bool):
                corpo.append((chave, "1" if v else ""))
            elif isinstance(v, dict):
                empilhar(chave, v)
            elif v is not None:
                corpo.append((chave, str(v)))

    empilhar("query_vars", qv)

    dados = urllib.parse.urlencode(corpo).encode()
    try:
        return _post(cfg["admin_ajax"], dados, categoria_url)
    except Exception:
        return ""


def _post(url, dados, referer):
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "Origin": BASE,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Connection": "close",
    }
    req = urllib.request.Request(url, data=dados, headers=headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT_PAGINA, context=_SSL_CTX) as r:
        return r.read().decode("utf-8", "ignore")


def coletar_categoria(slug):
    """Coleta todos os canais de uma categoria, paginando ate o fim."""
    url = f"{BASE}/video-genero/{slug}/"
    try:
        pagina = baixar(url)
    except Exception as e:
        log(f"   ! falha ao abrir categoria {slug}: {e}")
        return slug, [], "erro"

    cfg = ler_config_ajax(pagina)
    primeiro = extrair_itens(pagina)
    vistos = {}
    for u, t in primeiro:
        vistos.setdefault(u, t)

    pagina_num = 1
    sem_novos = 0
    total_paginas = 0
    m = re.findall(r'data-total-pages="(\d+)"', pagina)
    if m:
        total_paginas = max(int(x) for x in m)

    while pagina_num < MAX_PAGINAS:
        pagina_num += 1
        html_ajax = clicar_carregar_mais(cfg, url, pagina_num)
        if not html_ajax.strip():
            break
        novos = extrair_itens(html_ajax)
        if not novos:
            break
        antes = len(vistos)
        for u, t in novos:
            vistos.setdefault(u, t)
        if len(vistos) == antes:
            sem_novos += 1
            if sem_novos >= 2:
                break
        else:
            sem_novos = 0

    log(f"   {slug:34} paginas={pagina_num:<3} canais={len(vistos)}")
    return slug, [(u, t) for u, t in vistos.items()], "ok"


def coletar_catalogo(categorias):
    """Percorre todas as categorias e devolve {url: {titulo, categoria}}."""
    catalogo = {}
    for slug in categorias:
        _, canais, status = coletar_categoria(slug)
        for url, titulo in canais:
            chave = url.rstrip("/")
            if chave not in catalogo:
                catalogo[chave] = {"titulo": titulo, "categorias": []}
            if slug not in catalogo[chave]["categorias"]:
                catalogo[chave]["categorias"].append(slug)
    return catalogo


# ---------------------------------------------------------------------------
# 3) EXTRACAO DO LINK DO STREAM
# ---------------------------------------------------------------------------

RE_VIDEO_URL = re.compile(r'"video_url":"((?:[^"\\]|\\.)*)"')
RE_MODE = re.compile(r'"video_mode":"([a-z]+)"')
RE_LOGO = re.compile(r'data-src="(https://canais\.tvonlineaovivo\.com\.br/wp-content/uploads/[^"]+?)"')
RE_LOGO_PLACEHOLDER = re.compile(r"logo(sbt|placeholder|default|padrao)", re.I)
RE_IFRAME_SRC = re.compile(r'src=\\?"(https:\\\\?/\\\\?/[^"\\]+|https://[^"\\]+)')


def _desescapar(s):
    return s.replace("\\/", "/").replace('\\"', '"').replace("\\r", "").replace("\\n", "")


def escolher_logo(chave_html):
    """
    O site usa uma unica imagem generica (placeholder 'logosbt') em quase todos
    os canais. Usar isso poria o logo do SBT em canais que nao sao o SBT, entao
    detectamos o placeholder e devolvemos vazio (o SS IPTV mostra o nome).
    """
    logos = RE_LOGO.findall(chave_html)
    logos = [l for l in logos if not RE_LOGO_PLACEHOLDER.search(l)]
    if not logos:
        return ""
    for pref in ("-420x237", "-300x169", "-768x432", "-800x450", "-1024x576"):
        for l in logos:
            if pref in l:
                return l
    for l in logos:
        if not re.search(r"-\d+x\d+\.", l):
            return l
    return logos[0]


def extrair_stream(url_canal):
    """Abre a pagina do canal e devolve os dados do stream."""
    try:
        pagina = baixar(url_canal, timeout=TIMEOUT_PAGINA)
    except urllib.error.HTTPError as e:
        return {"status": f"pagina_http_{e.code}"}
    except Exception:
        return {"status": "pagina_erro"}

    modo_m = RE_MODE.search(pagina)
    modo = modo_m.group(1) if modo_m else "pro"
    logo = escolher_logo(pagina)

    alts = []
    base_canal = url_canal.rstrip("/")
    for g, n in sorted(set(re.findall(r'\?ml-group=(\d+)&#038;ml-url=(\d+)', pagina))):
        alts.append(f"{base_canal}?ml-group={g}&ml-url={n}")

    m = RE_VIDEO_URL.search(pagina)
    if not m:
        return {"status": "sem_video_url", "logo": logo, "modo": modo}

    bruto = _desescapar(m.group(1)).strip()

    if modo == "pro" and bruto.lower().startswith("http"):
        return {"status": "ok", "modo": "pro", "url": bruto,
                "logo": logo, "alternativas": alts}

    # Modo embed: YouTube / iframe -> nao toca no SS IPTV
    if "youtube" in bruto.lower() or "youtu.be" in bruto.lower():
        return {"status": "youtube", "logo": logo, "modo": modo, "detalhe": bruto[:160]}
    if bruto.lower().startswith("http"):
        return {"status": "embed", "logo": logo, "modo": modo, "detalhe": bruto[:160]}
    # video_mode pro mas o conteudo e um iframe colado
    mm = re.search(r'src=\\?"([^"\\]+)', bruto)
    if mm:
        src = _desescapar(mm.group(1))
        if "youtube" in src.lower():
            return {"status": "youtube", "logo": logo, "modo": "embed", "detalhe": src[:160]}
        return {"status": "embed", "logo": logo, "modo": "embed", "detalhe": src[:160]}
    return {"status": "embed", "logo": logo, "modo": modo, "detalhe": bruto[:160]}


# ---------------------------------------------------------------------------
# 4) TESTE DE REPRODUCAO
# ---------------------------------------------------------------------------

RE_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _urls_do_manifesto(texto, url_base):
    urls = []
    linhas = [l.strip() for l in texto.splitlines() if l.strip()]
    for i, linha in enumerate(linhas):
        if linha.startswith("#EXT-X-STREAM-INF") or linha.startswith("#EXT-X-MEDIA"):
            m = re.search(r'URI="([^"]+)"', linha)
            if m:
                urls.append(urllib.parse.urljoin(url_base, m.group(1)))
            elif linha.startswith("#EXT-X-STREAM-INF") and i + 1 < len(linhas):
                nxt = linhas[i + 1]
                if not nxt.startswith("#"):
                    urls.append(urllib.parse.urljoin(url_base, nxt))
    return urls


def testar_stream(url):
    """
    Testa se o canal realmente toca.
    Estrategia: baixa o manifesto HLS e, se for um master, baixa tambem uma
    variante/track e procura por segmentos nos ultimos ~120 KB do arquivo
    (um segmento presente = stream vivo de verdade).
    """
    if not url:
        return False, "sem_url"
    try:
        texto = baixar(url, timeout=TIMEOUT_STREAM)
    except urllib.error.HTTPError as e:
        return False, f"http_{e.code}"
    except (socket.timeout, TimeoutError):
        return False, "timeout"
    except Exception:
        return False, "erro_rede"

    if "#EXTM3U" not in texto[:200].upper():
        return False, "nao_e_m3u8"

    if "#EXT-X-STREAM-INF" not in texto:
        # Media playlist: basta ter segmentos listados
        if re.search(r"^#EXTINF", texto, re.M):
            return True, "ok"
        return False, "sem_segmentos"

    variantes = _urls_do_manifesto(texto, url)
    if not variantes:
        return False, "sem_variantes"

    for v in variantes[:3]:
        try:
            mid = baixar(v, timeout=TIMEOUT_STREAM)
        except Exception:
            continue
        if "#EXTM3U" not in mid[:200].upper():
            continue
        if re.search(r"^#EXTINF", mid, re.M):
            return True, "ok"

    return False, "sem_segmentos"


def testar_com_alternativas(info):
    url = info.get("url") or ""
    ok, motivo = testar_stream(url)
    if ok:
        return True, "ok", url
    for alt in info.get("alternativas", [])[:3]:
        try:
            pagina = baixar(alt, timeout=TIMEOUT_PAGINA)
            m = RE_VIDEO_URL.search(pagina)
            if not m:
                continue
            cand = _desescapar(m.group(1)).strip()
            if not cand.lower().startswith("http"):
                continue
            ok2, motivo2 = testar_stream(cand)
            if ok2:
                return True, "ok_alternativo", cand
        except Exception:
            continue
    return False, motivo, url


# ---------------------------------------------------------------------------
# 5) GERACAO DO M3U
# ---------------------------------------------------------------------------

ORDEM_CATEGORIAS = [
    "Geral", "Notícias", "Entretenimento", "Filmes", "Séries", "Infantil",
    "Animação", "Esportes", "Música", "Religioso", "Cultura", "Educação",
    "Legislativo", "Clássicos", "Família", "Estilo de Vida",
]


def ordem_categoria(nome):
    for i, c in enumerate(ORDEM_CATEGORIAS):
        if nome.lower().startswith(c.lower()):
            return i
    return len(ORDEM_CATEGORIAS) + 1


def escapar_attr(txt):
    return (txt or "").replace("\n", " ").replace('"', "'")


def escrever_m3u(canais, caminho):
    agora = time.strftime("%d/%m/%Y %H:%M")
    linhas = [
        "#EXTM3U",
        f'#PLAYLIST:TV Online Ao Vivo — atualizado em {agora}',
    ]

    def ordenar(ch):
        return (ch["ordem_cat"], normalizar(ch["categoria"]), normalizar(ch["titulo"]))

    for ch in sorted(canais, key=ordenar):
        nome = escapar_attr(ch["titulo"])
        grupo = escapar_attr(ch["categoria"])
        tvg_id = escapar_attr(ch["tvg_id"])
        logo = escapar_attr(ch.get("logo", ""))
        linhas.append(
            '#EXTINF:-1 tvg-id="{id}" tvg-name="{nome}" tvg-logo="{logo}" '
            'group-title="{grupo}" content-type="video",{nome}'.format(
                id=tvg_id, nome=nome, logo=logo, grupo=grupo)
        )
        linhas.append(ch["url"])

    with io.open(caminho, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(linhas) + "\n")


def gerar_epg(canais, caminho):
    """EPG simples (XMLTV) com a programacao 'Ao Vivo' para o SS IPTV."""
    agora = time.strftime("%Y%m%d%H%M%S +0000", time.gmtime())
    fim = time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(time.time() + 86400))
    partes = ['<?xml version="1.0" encoding="UTF-8"?>',
              '<tv generator-info-name="TV Online Ao Vivo">']
    for ch in canais:
        partes.append(f'  <channel id="{ch["tvg_id"]}"><display-name>{html.escape(ch["titulo"])}</display-name></channel>')
    for ch in canais:
        partes.append(
            f'  <programme start="{agora}" stop="{fim}" channel="{ch["tvg_id"]}">'
            f'<title lang="pt">Ao Vivo</title>'
            f'<desc lang="pt">Transmissão ao vivo de {html.escape(ch["titulo"])}</desc>'
            f'</programme>')
    partes.append("</tv>")
    with io.open(caminho, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(partes) + "\n")


# ---------------------------------------------------------------------------
# 6) RECONCILIACAO (manter / remover apos N falhas / adicionar novos)
# ---------------------------------------------------------------------------


def carregar_estado(caminho):
    if not os.path.exists(caminho):
        return {}
    try:
        with io.open(caminho, encoding="utf-8") as f:
            dados = json.load(f)
        return dados.get("canais", {}) if isinstance(dados, dict) else {}
    except Exception:
        log("   ! estado anterior ilegivel, comecando do zero")
        return {}


def salvar_json(caminho, obj):
    with io.open(caminho, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# PRINCIPAL
# ---------------------------------------------------------------------------


def main():
    inicio = time.time()
    log("=" * 74)
    log(" TV ONLINE AO VIVO -> M3U para SS IPTV")
    log(f" Inicio: {time.strftime('%d/%m/%Y %H:%M:%S')}")
    log("=" * 74)

    caminho_estado = os.path.join(DIR_SCRIPT, ARQ_ESTADO)
    caminho_lista = os.path.join(DIR_SCRIPT, ARQ_LISTA)
    caminho_rel = os.path.join(DIR_SCRIPT, ARQ_RELATORIO)

    estado = carregar_estado(caminho_estado)

    # ---- 1. categorias -----------------------------------------------------
    log("\n[1/5] Lendo as categorias do site...")
    try:
        categorias = listar_categorias()
    except Exception as e:
        log(f"!! Nao foi possivel ler as categorias: {e}")
        categorias = []

    if not INCLUIR_INTERNACIONAIS:
        categorias = [c for c in categorias if "brazil" in c or "/" not in c]

    if not categorias:
        log("!! Nenhuma categoria encontrada. Abortando sem apagar a lista existente.")
        return 1

    # ---- 2. catalogo -------------------------------------------------------
    log("\n[2/5] Coletando canais (1a pagina + 'Carregar mais' ate o fim)...")
    catalogo = coletar_catalogo(categorias)
    log(f"\n   Total de canais unicos encontrados no site: {len(catalogo)}")

    if len(catalogo) < 5:
        log("!! Catalogo praticamente vazio (site fora do ar ou bloqueado).")
        log("   Mantendo a lista atual sem alteracoes para nao apagar canais bons.")
        return 1

    # ---- 3. extrai streams -------------------------------------------------
    log("\n[3/5] Extraindo o link de video de cada canal...")
    itens = list(catalogo.items())

    def _extrair(kv):
        url, meta = kv
        info = extrair_stream(url)
        info["pagina"] = url
        info["titulo"] = meta["titulo"]
        info["categorias"] = meta["categorias"]
        return info

    with ThreadPoolExecutor(max_workers=THREADS_CATALOGO) as ex:
        infos = list(ex.map(_extrair, itens))

    por_status = {}
    for i in infos:
        por_status[i["status"]] = por_status.get(i["status"], 0) + 1
    log("   Situacao dos canais:")
    for k, v in sorted(por_status.items(), key=lambda x: -x[1]):
        log(f"     {k:16} {v}")

    # ---- 4. teste de reproducao -------------------------------------------
    log("\n[4/5] Testando se os links estao tocando...")
    candidatos = [i for i in infos if i["status"] in ("ok", "ok_alternativo") or i.get("url")]

    def _testar(info):
        if info["status"] not in ("ok",) and not info.get("url"):
            return info
        ok, motivo, urlfinal = testar_com_alternativas(info)
        info["toca"] = ok
        info["motivo"] = motivo
        if ok:
            info["url"] = urlfinal
            info["status"] = "tocando"
        else:
            info["status"] = "parado"
        return info

    with ThreadPoolExecutor(max_workers=THREADS_TESTE) as ex:
        infos = list(ex.map(_testar, infos))

    tocando = [i for i in infos if i.get("toca")]
    nao_tocando = [i for i in infos if i["status"] == "parado"]
    embed = [i for i in infos if i["status"] in ("youtube", "embed")]

    log(f"\n   TOCANDO ............ {len(tocando)}")
    log(f"   NAO TOCANDO ........ {len(nao_tocando)}  (contam falha)")
    log(f"   YOUTUBE/EMBED ...... {len(embed)}  (nao tocam no SS IPTV, ignorados)")

    if LOG_DETALHADO:
        log("\n   --- canais OK ---")
        for i in sorted(tocando, key=lambda x: normalizar(x["titulo"])):
            log(f"     [OK]   {i['titulo'][:40]:42} {(i.get('url') or '')[:58]}")
        log("\n   --- canais com problema ---")
        for i in sorted(nao_tocando + embed, key=lambda x: normalizar(x["titulo"])):
            log(f"     [--]   {i['titulo'][:40]:42} {i['status']:14} {i.get('motivo','') or i.get('detalhe','')[:30]}")

    # ---- 5. reconciliacao --------------------------------------------------
    log("\n[5/5] Reconciliando com a lista anterior...")
    novo_estado = {}
    mantidos = removidos = adicionados = 0

    def chave_estado(info):
        return normalizar(info["titulo"]) or info.get("pagina", "")

    # indexa estado antigo por chave normalizada
    antigos = {}
    for k, v in estado.items():
        antigos[normalizar(v.get("titulo", "")) or k] = v

    # canais que estao tocando agora entram/sao mantidos
    for info in tocando:
        k = chave_estado(info)
        antigo = antigos.pop(normalizar(info["titulo"]), None)
        novo_estado[k] = {
            "titulo": info["titulo"],
            "url": info.get("url", ""),
            "logo": info.get("logo", ""),
            "pagina": info.get("pagina", ""),
            "categorias": info.get("categorias", []),
            "falhas": 0,
            "ultimo_ok": time.strftime("%Y-%m-%d %H:%M"),
            "primeira_vez": (antigo or {}).get("primeira_vez", time.strftime("%Y-%m-%d %H:%M")),
        }
        if antigo:
            mantidos += 1
        else:
            adicionados += 1
            log(f"     + NOVO .... {info['titulo']}")

    # canais que estavam na lista e agora falharam
    falharam = {normalizar(i["titulo"]): i for i in nao_tocando}
    for chave_norm, velho in antigos.items():
        atual = falharam.get(chave_norm)
        falhas = int(velho.get("falhas", 0))
        # se o canal nem apareceu mais no site, tambem conta falha
        falhas += 1
        if falhas >= LIMITE_FALHAS:
            removidos += 1
            log(f"     - REMOVIDO  {velho.get('titulo')}  ({falhas} falhas seguidas)")
            continue
        registro = dict(velho)
        registro["falhas"] = falhas
        registro["ultimo_status"] = (atual or {}).get("motivo", "nao_encontrado_no_site")
        novo_estado[chave_norm] = registro
        if atual:
            log(f"     ~ AGUARDANDO {velho.get('titulo')}  (falha {falhas}/{LIMITE_FALHAS})")
        else:
            log(f"     ? SUMIU DO SITE {velho.get('titulo')}  (falha {falhas}/{LIMITE_FALHAS})")

    # monta a lista final: usa o estado (inclui os aguardando) mas apenas com url
    canais_m3u = []
    for k, v in novo_estado.items():
        url = v.get("url") or ""
        if not url:
            continue
        cats = v.get("categorias") or []
        cat_slug = cats[0] if cats else "geral"
        # prefere a categoria mais especifica (brasil) e mais "principal"
        for c in cats:
            if "brazil" in c:
                cat_slug = c
                break
        nome_cat = nome_categoria_amigavel(cat_slug, CATEGORIAS_PT)
        canais_m3u.append({
            "titulo": v["titulo"],
            "url": url,
            "logo": v.get("logo", ""),
            "categoria": nome_cat,
            "ordem_cat": ordem_categoria(nome_cat),
            "tvg_id": (re.sub(r"[^A-Za-z0-9]+", "", v["titulo"]).title() or "Canal"),
        })

    # deduplica por url (mesmo link = um so canal)
    vistos_url, finais = set(), []
    for c in canais_m3u:
        if c["url"] in vistos_url:
            continue
        vistos_url.add(c["url"])
        finais.append(c)

    if not finais:
        log("\n!! Nenhum canal tocando nesta execucao. A lista anterior foi preservada.")
        salvar_json(caminho_estado, {"atualizado": time.strftime("%Y-%m-%d %H:%M"), "canais": novo_estado})
        return 1

    escrever_m3u(finais, caminho_lista)
    gerar_epg(finais, os.path.join(DIR_SCRIPT, "tvonlineaovivo.xml"))
    salvar_json(caminho_estado, {
        "atualizado": time.strftime("%Y-%m-%d %H:%M"),
        "limite_falhas": LIMITE_FALHAS,
        "canais": novo_estado,
    })

    categorias_final = {}
    for c in finais:
        categorias_final[c["categoria"]] = categorias_final.get(c["categoria"], 0) + 1

    resumo = {
        "atualizado": time.strftime("%Y-%m-%d %H:%M:%S"),
        "canais_no_site": len(catalogo),
        "canais_tocando": len(tocando),
        "canais_sem_play": len(nao_tocando),
        "youtube_embed_ignorados": len(embed),
        "na_lista_final": len(finais),
        "novos": adicionados,
        "mantidos": mantidos,
        "removidos": removidos,
        "por_categoria": dict(sorted(categorias_final.items(), key=lambda x: -x[1])),
        "duracao_segundos": round(time.time() - inicio, 1),
    }
    salvar_json(caminho_rel, resumo)

    log("\n" + "=" * 74)
    log(f" LISTA GERADA: {ARQ_LISTA}")
    log(f" Canais na lista .......... {len(finais)}")
    log(f" Novos .................... {adicionados}")
    log(f" Mantidos ................. {mantidos}")
    log(f" Removidos (6 falhas) ..... {removidos}")
    log("\n Canais por categoria:")
    for cat, qtd in resumo["por_categoria"].items():
        log(f"   {cat:22} {qtd}")
    log(f"\n Tempo total: {resumo['duracao_segundos']}s")
    log("=" * 74)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("\nInterrompido pelo usuario.")
        sys.exit(130)
