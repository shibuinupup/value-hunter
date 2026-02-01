import os
import re
import time
import uuid
import json
import queue
import threading
import requests
from flask import Flask, render_template, request, Response, redirect, url_for
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)
# --- ENV ---
COVALENT_API_KEY = os.getenv("COVALENT_API_KEY")
ZEROX_API_KEY = os.getenv("ZEROX_API_KEY")
CHAIN_ID = int(os.getenv("CHAIN_ID", "1"))

BUY_TOKEN_USDC = os.getenv("BUY_TOKEN_USDC")
BUY_TOKEN_WETH = os.getenv("BUY_TOKEN_WETH")  # ETH route uses WETH address
MIN_USDC = float(os.getenv("MIN_USDC", "0.5"))

if not all([COVALENT_API_KEY, ZEROX_API_KEY, BUY_TOKEN_USDC, BUY_TOKEN_WETH]):
    raise SystemExit(
        "Missing env vars. Need: COVALENT_API_KEY, ZEROX_API_KEY, BUY_TOKEN_USDC, BUY_TOKEN_WETH"
    )

# --- Flask ---
app = Flask(__name__)
session = requests.Session()

# --- Validation ---
ETH_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

# --- 0x Swap API v2 (AllowanceHolder) ---
ZEROX_PRICE_URL = "https://api.0x.org/swap/allowance-holder/price"
ZEROX_HEADERS = {"0x-api-key": ZEROX_API_KEY, "0x-version": "v2"}

# --- In-memory job store (local dev) ---
JOBS = {}
JOBS_LOCK = threading.Lock()


def is_eth_address(s: str) -> bool:
    return bool(ETH_ADDR_RE.match((s or "").strip()))


def covalent_tokens(wallet: str):
    url = f"https://api.covalenthq.com/v1/{CHAIN_ID}/address/{wallet}/balances_v2/"
    r = session.get(
        url,
        params={"key": COVALENT_API_KEY, "no-nft-fetch": "true"},
        timeout=25,
    )
    r.raise_for_status()
    return r.json()["data"]["items"]


def zerox_price(wallet: str, sell_token: str, sell_amount_raw: int, buy_token_addr: str):
    params = {
        "chainId": str(CHAIN_ID),
        "sellToken": sell_token,
        "buyToken": buy_token_addr,
        "sellAmount": str(sell_amount_raw),
        "taker": wallet,
    }
    try:
        r = session.get(ZEROX_PRICE_URL, params=params, headers=ZEROX_HEADERS, timeout=18)
    except Exception:
        return None

    if r.status_code != 200:
        return None

    j = r.json()
    buy = j.get("buyAmount")
    if not buy or buy == "0":
        return None
    return j


def amount_from_buy_amount(buy_amount_raw: str, buy_mode: str) -> float:
    # USDC has 6 decimals, WETH has 18 decimals
    if buy_mode == "USDC":
        return int(buy_amount_raw) / 1e6
    return int(buy_amount_raw) / 1e18  # WETH ≈ ETH


def matcha_link(sell_token: str, sell_amount_raw: int, buy_token_addr: str) -> str:
    return (
        "https://matcha.xyz/swap?"
        f"chainId={CHAIN_ID}&sellToken={sell_token}&buyToken={buy_token_addr}&sellAmount={sell_amount_raw}"
    )


def evaluate_token_fast(wallet: str, token_addr: str, balance_raw: int, buy_mode: str, buy_token_addr: str):
    """
    FAST heuristic:
      - try 1%, 0.1%, 0.01% of balance
      - if none, micro=1 to detect traps
    Returns dict with status + out_est + link/note.
    """
    frac_amounts = [
        max(int(balance_raw * 0.01), 1),
        max(int(balance_raw * 0.001), 1),
        max(int(balance_raw * 0.0001), 1),
    ]

    # Threshold:
    # - USDC uses MIN_USDC
    # - ETH uses a rough ETH threshold (approx ~$0.50). Adjust if you want.
    min_out = MIN_USDC if buy_mode == "USDC" else 0.0002

    for amt in frac_amounts:
        q = zerox_price(wallet, token_addr, amt, buy_token_addr)
        if not q:
            continue
        out_amt = amount_from_buy_amount(q["buyAmount"], buy_mode)
        if out_amt >= min_out:
            return {
                "status": "PARTIAL",
                "out_est": out_amt,
                "sell_amount_raw": amt,
                "link": matcha_link(token_addr, amt, buy_token_addr),
                "note": ""
            }

    # micro trap check
    q1 = zerox_price(wallet, token_addr, 1, buy_token_addr)
    if q1:
        out1 = amount_from_buy_amount(q1["buyAmount"], buy_mode)
        if out1 >= min_out:
            return {
                "status": "MICRO_ONLY",
                "out_est": out1,
                "sell_amount_raw": 1,
                "link": "",  # no link for micro-only (avoid misleading)
                "note": "Only micro amounts hit threshold (likely trap/maxTx/tax)."
            }
        elif out1 > 0:
            return {
                "status": "DUST_ONLY",
                "out_est": out1,
                "sell_amount_raw": 1,
                "link": "",
                "note": ""
            }

    return {"status": "NO_ROUTE", "out_est": 0.0, "sell_amount_raw": None, "link": "", "note": ""}


def push(job_id: str, event: str, payload: dict):
    with JOBS_LOCK:
        q = JOBS.get(job_id, {}).get("q")
    if not q:
        return
    payload = payload or {}
    payload["event"] = event
    q.put(payload)


def run_scan(job_id: str, wallets: list[str], buy_mode: str):
    start_ts = time.time()

    buy_mode = (buy_mode or "ETH").upper()
    if buy_mode not in ["USDC", "ETH"]:
        buy_mode = "ETH"

    buy_token_addr = BUY_TOKEN_USDC if buy_mode == "USDC" else BUY_TOKEN_WETH

    push(job_id, "meta", {
        "chainId": CHAIN_ID,
        "minUsdc": MIN_USDC,
        "wallets": wallets,
        "startedAt": start_ts,
        "buyMode": buy_mode
    })

    output = []
    errors = []

    for w_i, wallet in enumerate(wallets, start=1):
        push(job_id, "wallet_start", {"wallet": wallet, "index": w_i, "total": len(wallets)})

        try:
            items = covalent_tokens(wallet)
        except Exception as e:
            msg = f"{wallet}: covalent fetch failed ({type(e).__name__})"
            errors.append(msg)
            push(job_id, "wallet_error", {"wallet": wallet, "msg": msg})
            continue

        rows = []
        scanned = 0
        found = 0

        for t in items:
            bal = int(t.get("balance", "0") or "0")
            if bal <= 0:
                continue

            sym = (t.get("contract_ticker_symbol") or "").strip() or "?"
            name = (t.get("contract_name") or "").strip()
            addr = t.get("contract_address")

            # skip native/wrapped and USDC itself
            if not addr or sym in ["ETH", "WETH", "USDC"]:
                continue

            scanned += 1
            push(job_id, "token", {"wallet": wallet, "symbol": sym, "name": name, "contract": addr, "stage": "quoting"})

            verdict = evaluate_token_fast(wallet, addr, bal, buy_mode, buy_token_addr)

            if verdict["status"] in ["PARTIAL", "MICRO_ONLY", "DUST_ONLY"]:
                found += 1

                row = {
                    "symbol": sym,
                    "name": name,
                    "token": addr,
                    "status": verdict["status"],
                    "out_est": verdict["out_est"],
                    "link": verdict["link"],
                    "note": verdict["note"],
                }
                rows.append(row)

                # ✅ Send full payload so UI can show result immediately
                push(job_id, "token_result", {
                    "wallet": wallet,
                    "symbol": sym,
                    "name": name,
                    "token": addr,
                    "status": verdict["status"],
                    "out_est": verdict["out_est"],
                    "link": verdict["link"],
                    "note": verdict["note"],
                })
            else:
                push(job_id, "token_result", {
                    "wallet": wallet,
                    "symbol": sym,
                    "name": name,
                    "token": addr,
                    "status": verdict["status"],
                    "out_est": 0,
                    "link": "",
                    "note": ""
                })

            time.sleep(0.04)

        rows.sort(key=lambda x: x.get("out_est", 0), reverse=True)
        output.append({"wallet": wallet, "rows": rows, "scanned": scanned, "found": found})

        push(job_id, "wallet_done", {"wallet": wallet, "scanned": scanned, "found": found})

    total_s = round(time.time() - start_ts, 2)

    with JOBS_LOCK:
        if job_id in JOBS:
            JOBS[job_id]["done"] = True
            JOBS[job_id]["output"] = output
            JOBS[job_id]["errors"] = errors
            JOBS[job_id]["buyMode"] = buy_mode

    push(job_id, "done", {"seconds": total_s, "errors": errors, "output": output, "buyMode": buy_mode})


@app.route("/", methods=["GET"])
def home():
    return render_template(
        "index.html",
        chain_id=CHAIN_ID,
        min_usdc=MIN_USDC,
        usdc_addr=BUY_TOKEN_USDC,
        weth_addr=BUY_TOKEN_WETH,
        job_id=None,
        wallets_text="",
        buy_mode="ETH",  # ✅ default ETH
    )


@app.route("/start", methods=["POST"])
def start():
    wallets_text = request.form.get("wallets", "")
    buy_mode = request.form.get("buy_mode", "ETH")  # ✅ default ETH

    wallets_raw = [w.strip() for w in wallets_text.splitlines() if w.strip()]
    valid = [w for w in wallets_raw if is_eth_address(w)]

    # 🔁 dedupe while preserving order
    seen = set()
    deduped = []
    for w in valid:
        wl = w.lower()
        if wl not in seen:
            seen.add(wl)
            deduped.append(w)

    valid = deduped

    job_id = uuid.uuid4().hex[:12]
    q = queue.Queue()

    with JOBS_LOCK:
        JOBS[job_id] = {
            "q": q,
            "done": False,
            "output": None,
            "errors": [],
            "wallets_text": wallets_text,
            "buyMode": buy_mode,
        }

    t = threading.Thread(target=run_scan, args=(job_id, valid, buy_mode), daemon=True)
    t.start()

    return redirect(url_for("job_view", job_id=job_id))


@app.route("/job/<job_id>", methods=["GET"])
def job_view(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return redirect(url_for("home"))

    return render_template(
        "index.html",
        chain_id=CHAIN_ID,
        min_usdc=MIN_USDC,
        usdc_addr=BUY_TOKEN_USDC,
        weth_addr=BUY_TOKEN_WETH,
        job_id=job_id,
        wallets_text=job.get("wallets_text", ""),
        buy_mode=(job.get("buyMode") or "ETH").upper(),  # ✅ default ETH
    )


@app.route("/events/<job_id>")
def events(job_id):
    def stream():
        while True:
            with JOBS_LOCK:
                job = JOBS.get(job_id)

            if not job:
                yield "event: error\ndata: {}\n\n"
                return

            try:
                msg = job["q"].get(timeout=0.8)
                ev = msg.pop("event", "message")
                yield f"event: {ev}\ndata: {json.dumps(msg)}\n\n"
            except queue.Empty:
                yield "event: ping\ndata: {}\n\n"

            with JOBS_LOCK:
                done = JOBS.get(job_id, {}).get("done", False)
            if done:
                time.sleep(0.2)
                return

    return Response(stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8000, debug=True, threaded=True)
