# collect_lula_onu_playwright.py
"""
Playwright-based scraper:
- busca variações de "Lula na ONU" no X (f=live)
- rola página, abre threads, coleta conversa
- detecta replies 'grok' (username/displayname/text contém 'grok')
- salva CSV + parquet em data/
"""

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
import time, os, json, uuid, re
from pathlib import Path
import pandas as pd
from dotenv import load_dotenv

load_dotenv()

# -------- CONFIG --------
OUT_DIR = Path("data")
OUT_DIR.mkdir(exist_ok=True)
BROWSER_DATA = Path(__file__).parent / "browser_data"   # persiste sessão
BROWSER_EXECUTABLE = os.getenv("BROWSER_PATH")  # opcional: caminho do Chrome/Edge/Chromium
TW_EMAIL = os.getenv("TW_EMAIL")
TW_USER = os.getenv("TW_USER")
TW_PASSWORD = os.getenv("TW_PASSWORD")

QUERIES = [
    'Lula na ONU lang:pt',
    'Lula ONU lang:pt',
    '"Lula na onu" lang:pt',
    'Lula UN lang:pt'
]
QTD_POSTS = 700       # total aproximado por query (ajusta)
SCROLL_PAUSE = 0.8
MAX_THREAD_TWEETS = 300
HEADLESS = False      # ver janela durante login/debug = False

# ---------- helpers ----------
def ts():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def log(msg):
    print(f"[{ts()}] {msg}", flush=True)

def text_to_num(txt):
    if not txt:
        return 0
    s = txt.strip().upper().replace(",", ".")
    try:
        if s.endswith("K"):
            return int(float(s[:-1]) * 1_000)
        if s.endswith("M"):
            return int(float(s[:-1]) * 1_000_000)
        return int(float(s))
    except:
        return 0

def is_grok_like(username, displayname, text):
    uname = (username or "").lower()
    dname = (displayname or "").lower()
    txt = (text or "").lower()
    return ("grok" in uname) or ("grok" in dname) or ("grok" in txt)

def make_search_url(term):
    import urllib.parse
    return f"https://x.com/search?q={urllib.parse.quote(term)}&src=typed_query&f=live"

# ---------- extraction helpers (page-level) ----------
def extract_tweet_from_article(article):
    """
    Recebe um Locator p/ <article> e extrai: tweet_id, href, username, displayname, text, time
    """
    try:
        # href / id
        href = article.locator("a[href*='/status/']").first.get_attribute("href")
        tweet_id = None
        if href:
            m = re.search(r"/status/(\d+)", href)
            if m: tweet_id = m.group(1)
        # text
        text = ""
        try:
            text = article.locator("div[data-testid='tweetText']").first.inner_text().strip()
        except Exception:
            text = article.inner_text().strip()
        # username / displayname - heurística
        username = None
        displayname = None
        try:
            # encontra todos os links que não sejam status (perfil)
            profile_link = article.locator("a").filter(has_text=lambda t: t is not None)
            # percorre anchors procurando href /twitter.com/{username}
            anchors = article.locator("a").all()
            for a in anchors:
                href_a = a.get_attribute("href") or ""
                if "/status/" in href_a:
                    continue
                m2 = re.search(r"https?://(?:www\.)?twitter\.com/([^/?#]+)", href_a)
                if m2:
                    username = m2.group(1)
                    break
            # displayname: pegar primeiro span visível dentro do header do article
            try:
                displayname = article.locator("div[dir='auto'] span").nth(0).inner_text().strip()
            except Exception:
                displayname = None
        except Exception:
            pass
        # time
        time_iso = None
        try:
            time_iso = article.locator("time").first.get_attribute("datetime")
        except Exception:
            time_iso = None
        return {
            "tweet_id": tweet_id,
            "href": href,
            "username": username,
            "displayname": displayname,
            "text": text,
            "time": time_iso
        }
    except Exception as e:
        log(f"extract_tweet_from_article exception: {e}")
        return None

def collect_thread(page, status_url, max_items=MAX_THREAD_TWEETS):
    """
    Abre o status_url em nova página/aba e coleta articles (tweet + replies).
    Retorna lista de dicts (mesmo formato de extract_tweet_from_article).
    """
    log(f"Collect thread: opening {status_url}")
    thread = []
    popup = page.context.new_page()
    try:
        popup.goto(status_url, timeout=30000)
        time.sleep(1.0)
        # rolar e coletar articles
        start = time.time()
        while True:
            articles = popup.locator("article[data-testid='tweet']")
            count = articles.count()
            for i in range(count):
                try:
                    art = articles.nth(i)
                    info = extract_tweet_from_article(art)
                    if info and info.get("tweet_id") and not any(d.get("tweet_id")==info.get("tweet_id") for d in thread):
                        thread.append(info)
                except Exception as e:
                    log(f"  warn: error extracting article in thread: {e}")
                    continue
            if len(thread) >= max_items:
                break
            # scroll by viewport height
            popup.evaluate("window.scrollBy(0, window.innerHeight);")
            time.sleep(0.6)
            if time.time() - start > 30:  # timeout por thread
                break
        return thread
    except Exception as e:
        log(f"collect_thread exception: {e}")
        return thread
    finally:
        try:
            popup.close()
        except:
            pass

# ---------- main flow ----------
def run():
    results = []
    with sync_playwright() as p:
        browser_type = p.chromium
        launch_args = {
            "headless": HEADLESS,
            "args": [
                "--no-first-run", "--no-default-browser-check",
                "--disable-session-crashed-bubble",
                "--disable-blink-features=AutomationControlled"
            ]
        }
        if BROWSER_EXECUTABLE:
            launch_args["executable_path"] = BROWSER_EXECUTABLE

        # cria contexto persistente (mantém login)
        context = browser_type.launch_persistent_context(user_data_dir=str(BROWSER_DATA),
                                                         **launch_args)
        page = context.new_page()
        try:
            # Se precisarmos fazer login automático (opcional)
            if TW_EMAIL and TW_PASSWORD:
                log("Attempting to open login page (manual flow will be accepted if 2FA needed).")
                page.goto("https://x.com/login", timeout=30000)
                time.sleep(1.0)
                # tenta preencher email/username e seguir fluxo
                try:
                    # diferentes UIs: teste por seletores
                    if page.locator("input[name='text']").is_visible(timeout=2000):
                        log("Filling email/user field.")
                        page.fill("input[name='text']", TW_EMAIL)
                        page.click("text='Avançar'")  # pt label; pode variar
                        time.sleep(1.0)
                    if page.locator("input[name='password']").is_visible(timeout=2000):
                        page.fill("input[name='password']", TW_PASSWORD)
                        page.click("text='Log in' or text='Entrar' or text='Log in'")  # tentativa
                        time.sleep(1.0)
                except PWTimeout:
                    log("Login selectors not found quickly — expect manual login if needed.")
                # se tiver modal de restauração etc, o contexto persistente tende a abrir
                # pausa para login manual / 2FA
                if "login" in page.url.lower():
                    log("Detected login page or not logged in. Please complete manual login in the opened browser window.")
                    input("Press Enter after you finish manual login (2FA) in the browser window...")
            else:
                log("No TW_EMAIL/TW_PASSWORD provided. If not logged, you must log manually in the browser window.")

            # loop de queries
            for q_idx, q in enumerate(QUERIES, start=1):
                log(f"=== Query {q_idx}/{len(QUERIES)}: {q} ===")
                search_url = make_search_url(q)
                log(f"Going to {search_url}")
                try:
                    page.goto(search_url, timeout=30000)
                except Exception as e:
                    log(f"Navigation exception to search_url: {e}. Retrying with simple twitter.com then search.")
                    try:
                        page.goto("https://x.com", timeout=20000)
                        time.sleep(1.0)
                        page.goto(search_url, timeout=30000)
                    except Exception as e2:
                        log(f"Failed to navigate to search for query {q}: {e2}")
                        continue

                # scroll + collect unique status links
                seen = set()
                start = time.time()
                while len(seen) < min(QTD_POSTS, 200) and time.time() - start < 90:
                    articles = page.locator("article[data-testid='tweet']")
                    n = articles.count()
                    log(f"  Found {n} article elements on page")
                    for i in range(n):
                        try:
                            art = articles.nth(i)
                            # pegar link status
                            link_handle = art.locator("a[href*='/status/']").first
                            href = link_handle.get_attribute("href")
                            if href:
                                seen.add(href)
                        except Exception:
                            continue
                    log(f"  Unique status links collected so far: {len(seen)}")
                    # scroll
                    page.evaluate("window.scrollBy(0, window.innerHeight);")
                    time.sleep(SCROLL_PAUSE)

                log(f"Collected total unique links: {len(seen)}")
                links = list(seen)[:QTD_POSTS]

                # Para cada link, abre thread (nova page) e coleta
                for idx, href in enumerate(links, start=1):
                    log(f"-- ({idx}/{len(links)}) Processing link: {href}")
                    conv = collect_thread(page, href, max_items=MAX_THREAD_TWEETS)
                    if not conv:
                        log("   No conversation collected (empty), skipping")
                        continue
                    # determinar root = earliest time if present
                    root_candidates = [c for c in conv if c.get("time")]
                    root = sorted(root_candidates, key=lambda x: x.get("time"))[0] if root_candidates else conv[0]
                    # procurar grok replies
                    grok_hits = [t for t in conv if is_grok_like(t.get("username"), t.get("displayname"), t.get("text"))]
                    if not grok_hits:
                        log("   No grok-like replies found in this thread")
                        continue
                    for gh in grok_hits:
                        rec = {
                            "id": str(uuid.uuid4()),
                            "query": q,
                            "root_tweet_id": root.get("tweet_id"),
                            "root_user": root.get("username"),
                            "root_text": root.get("text"),
                            "root_time": root.get("time"),
                            "grok_tweet_id": gh.get("tweet_id"),
                            "grok_user": gh.get("username"),
                            "grok_displayname": gh.get("displayname"),
                            "grok_text": gh.get("text"),
                            "grok_time": gh.get("time"),
                            "conversation": conv
                        }
                        results.append(rec)
                    # save incremental
                    if len(results) > 0 and len(results) % 10 == 0:
                        try:
                            df_tmp = pd.DataFrame(results)
                            df_tmp["conversation"] = df_tmp["conversation"].apply(lambda x: json.dumps(x, ensure_ascii=False))
                            fname = OUT_DIR / "classA_lula_onu_grok_hits_partial.parquet"
                            df_tmp.to_parquet(fname, index=False)
                            log(f"  incremental saved {len(results)} hits -> {fname}")
                        except Exception as e:
                            log(f"  incremental save failed: {e}")
                # small pause between queries
                log(f"Finished query {q_idx}. Pausing briefly.")
                time.sleep(1.0)

        finally:
            # final save
            if results:
                df = pd.DataFrame(results)
                df["conversation"] = df["conversation"].apply(lambda x: json.dumps(x, ensure_ascii=False))
                ts_now = int(time.time())
                pfn = OUT_DIR / f"classA_lula_onu_grok_hits_{ts_now}.parquet"
                cfn = OUT_DIR / f"classA_lula_onu_grok_hits_{ts_now}.csv"
                df.to_parquet(pfn, index=False)
                df.to_csv(cfn, index=False)
                log(f"Final saved {len(results)} hits -> {pfn}, {cfn}")
            else:
                log("No hits collected.")
            try:
                context.close()
            except Exception:
                pass

if __name__ == "__main__":
    run()
