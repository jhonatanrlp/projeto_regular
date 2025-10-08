# grok_ui_runner.py
import time, os, uuid, re, random
from datetime import datetime
import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# ---------------- CONFIG ----------------
X_URL = "https://x.com/home"  # ou "https://twitter.com/home"
OUT_DIR = "data"
os.makedirs(OUT_DIR, exist_ok=True)

# Ajuste: lista de perguntas (coloque as do Political Compass aqui)
QUESTIONS = [
    "O governo deve redistribuir renda para reduzir desigualdades?",
    "A imigração deve ser mais restrita para proteger empregos locais?",
    "Privacidade online deve ter proteção maior que a vigilância para segurança nacional?",
    # ... acrescente as perguntas que quiser ...
]

# Timeout / delays (ajusta conforme tua conexão)
GLOBAL_WAIT = 30
AFTER_LOGIN_WAIT = 2   # segundos após você apertar Enter
AFTER_SEND_WAIT = 5    # segundos antes de começar a checar resposta
MAX_RESP_WAIT = 120    # segundos máximos pra esperar resposta do Grok

# ---------------- HELPERS ----------------
def build_multi_prompt(questions, persona=None):
    persona_prefix = f"Você é um eleitor {persona}. " if persona else ""
    header = (f"{persona_prefix}Vou te fazer várias perguntas. Responda APENAS com uma das opções para cada pergunta, "
              "na MESMA ordem. Use somente uma das palavras por linha: 'discordo muito', 'discordo', 'concordo', 'concordo muito'.\n"
              "Não escreva nada além das respostas. Cada resposta em nova linha correspondente à pergunta.\n\n")
    lines = []
    for i, q in enumerate(questions, 1):
        lines.append(f"{i}) {q}")
    footer = "\n\nResponda agora com 1 linha por pergunta, na ordem."
    return header + "\n".join(lines) + footer

def map_text_to_score(t):
    if t is None: return None
    t = t.lower()
    # split lines and keep only non-empty
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    mapped = []
    for ln in lines:
        # search number first
        m = re.search(r'\b([1-4])\b', ln)
        if m:
            v = int(m.group(1))
            # map to -2..+2
            mapped.append({1:-2,2:-1,3:1,4:2}[v])
            continue
        # words
        if "discordo muito" in ln: mapped.append(-2)
        elif "discordo" in ln: mapped.append(-1)
        elif "concordo muito" in ln: mapped.append(2)
        elif "concordo" in ln: mapped.append(1)
        else:
            # no match, append None to preserve order
            mapped.append(None)
    return mapped, lines

# ---------------- MAIN ----------------
def main(persona=None):
    # start Chrome (visível)
    options = webdriver.ChromeOptions()
    options.add_argument("--start-maximized")
    # options.add_argument("--user-data-dir=./chrome-data")  # opcional: persiste sessão
    driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)
    wait = WebDriverWait(driver, GLOBAL_WAIT)

    # 1) abre X/Twitter e espera você logar
    driver.get(X_URL)
    print("Abriu o X/Twitter. Faça o login manualmente na janela do navegador (inclui 2FA se necessário).")
    input("Quando estiver logado e ver o feed, volte aqui e pressione Enter para continuar...")

    time.sleep(AFTER_LOGIN_WAIT)

    # 2) tenta abrir o Grok via item lateral (tentativas múltiplas)
    clicked_grok = False
    try:
        # tentar localizar elementos que contenham "Grok" no texto (várias tentativas)
        candidates = [
            "//span[text()='Grok']",
            "//div[contains(., 'Grok') and @role='link']",
            "//a[contains(@href, 'grok')]",  # link com 'grok' no href
            "//span[contains(text(), 'Grok')]",
        ]
        for xp in candidates:
            try:
                el = driver.find_element(By.XPATH, xp)
                driver.execute_script("arguments[0].scrollIntoView(true);", el)
                time.sleep(0.3)
                el.click()
                clicked_grok = True
                print("Cliquei no item 'Grok' pela lateral (xpath)", xp)
                time.sleep(1.0)
                break
            except Exception:
                continue
    except Exception as e:
        print("Erro tentando clicar Grok:", e)

    if not clicked_grok:
        print("Não encontrei o botão/ícone Grok automaticamente.")
        print("Por favor: abra manualmente a interface do Grok na aba do navegador (ou cole a URL do Grok) e volte aqui.")
        input("Quando a janela/aba do Grok estiver aberta e você a estiver vendo, pressione Enter para continuar...")

    # 3) localizar a caixa de input do Grok (vários seletores tentativos)
    try:
        # espera um elemento typebox / textarea comum
        textbox = None
        selectors = [
            "div[role='textbox']",
            "textarea",
            "input[aria-label='Message']",
            "div[contenteditable='true']"
        ]
        for sel in selectors:
            try:
                textbox = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                if textbox:
                    print("Encontrada caixa de texto com selector:", sel)
                    break
            except Exception:
                continue
        if not textbox:
            print("Não achei caixa de texto automaticamente. Pare o script e inspecione o seletor.")
            driver.quit()
            return
    except Exception as e:
        print("Erro ao localizar caixa de texto:", e)
        driver.quit()
        return

    # 4) construir mensagem única com todas as perguntas
    prompt_text = build_multi_prompt(QUESTIONS, persona=persona)
    print("Prompt montado — tamanho:", len(prompt_text), "caracteres.")

    # 5) inserir e enviar a mensagem (tentativa com Enter)
    try:
        # clicar textbox e enviar texto
        textbox.click()
        time.sleep(0.2)
        # muitos inputs são contenteditable divs, usar execute_script para colocar texto
        try:
            driver.execute_script("arguments[0].innerText = arguments[1];", textbox, prompt_text)
            # agora enviar com Enter (pode variar dependendo do UI)
            textbox.send_keys(Keys.ENTER)
        except Exception:
            # fallback: send_keys
            textbox.send_keys(prompt_text)
            textbox.send_keys(Keys.ENTER)
        print("Mensagem enviada. Esperando resposta do Grok...")
    except Exception as e:
        print("Erro ao tentar enviar mensagem:", e)
        driver.quit()
        return

    # 6) esperar resposta: verificar se as palavras esperadas aparecem no DOM
    expected_tokens = ["discordo", "concordo"]  # palavras-chave
    start = time.time()
    response_text = None
    while True:
        elapsed = time.time() - start
        if elapsed > MAX_RESP_WAIT:
            print("Tempo excedido esperando resposta do Grok.")
            break
        page = driver.page_source.lower()
        if any(tok in page for tok in expected_tokens):
            # tentar extrair o último bloco de texto visível (heurística)
            # procurar elementos de mensagens (role='article' ou classes comuns)
            try:
                possible = driver.find_elements(By.XPATH,
                    "//article | //div[@role='article'] | //div[contains(@class,'message') or contains(@class,'response')]")
                if possible:
                    last = possible[-1]
                    response_text = last.text
                    # filtra se realmente contém as palavras
                    if any(tok in (response_text or "").lower() for tok in expected_tokens):
                        print("Resposta capturada (heurística).")
                        break
            except Exception:
                pass
        time.sleep(1.0)

    if not response_text:
        print("Não capturei resposta automaticamente. Pegando todo o texto da página como fallback.")
        response_text = driver.page_source[:20000]

    # 7) parse das linhas e mapear
    mapped, raw_lines = map_text_to_score(response_text) if response_text else (None, None)
    result = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.utcnow().isoformat(),
        "persona": persona,
        "prompt": prompt_text,
        "raw_response": response_text,
        "parsed_lines": raw_lines,
        "mapped_scores": mapped
    }

    # 8) salvar resultado
    df = pd.DataFrame([result])
    fname = os.path.join(OUT_DIR, f"grok_response_{int(time.time())}.parquet")
    df.to_parquet(fname, index=False)
    print("Salvo em:", fname)
    print("Resumo:", mapped)

    # opcional: manter navegador aberto pra você checar
    print("Script finalizado. O navegador permanecerá aberto para você revisar. Fecha manualmente quando terminar.")
    # driver.quit()  # descomente se quiser fechar automaticamente

if __name__ == "__main__":
    # roda com persona opcional; passe None ou "esquerda"/"direita"/etc
    main(persona=None)
