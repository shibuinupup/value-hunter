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

# =========================
# ENV
# =========================
COVALENT_API_KEY = os.getenv("COVALENT_API_KEY")
ZEROX_API_KEY = os.getenv("ZEROX_API_KEY")

DEFAULT_CHAIN_ID = int(os.getenv("CHAIN_ID", "1"))

# Thresholds
MIN_USDC = float(os.getenv("MIN_USDC", "0.5"))
DUST_MIN_USDC = float(os.getenv("DUST_MIN_USDC", "0.01"))

# Used when buy_mode != USDC (UI calls this "ETH" mode). This is in "wrapped native" units.
MIN_WRAPPED_NATIVE = float(os.getenv("MIN_WRAPPED_NATIVE", "0.0002"))

if not all([COVALENT_API_KEY, ZEROX_API_KEY]):
    raise SystemExit("Missing env vars. Need: COVALENT_API_KEY, ZEROX_API_KEY")

session = requests.Session()

# =========================
# Chain config
# =========================
# Notes:
# - USDC decimals are usually 6 (Circle native), but on BSC the commonly-used USDC is often 18.
# - "ETH" mode in the UI means "wrapped native route" (WETH/WBNB/WMATIC/WAVAX/etc depending on chain).
CHAIN_CONFIG = {
    1: {
        "name": "Ethereum",
        "native_symbol": "ETH",
        "stable": {"symbol": "USDC", "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48", "decimals": 6},
        "wrapped": {"symbol": "WETH", "address": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", "decimals": 18},
    },
    10: {
        "name": "Optimism",
        "native_symbol": "ETH",
        "stable": {"symbol": "USDC", "address": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85", "decimals": 6},
        "wrapped": {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
    },
    56: {
        "name": "BSC",
        "native_symbol": "BNB",
        "stable": {"symbol": "USDC", "address": "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", "decimals": 18},
        "wrapped": {"symbol": "WBNB", "address": "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c", "decimals": 18},
    },
    137: {
        "name": "Polygon",
        "native_symbol": "POL",
        "stable": {"symbol": "USDC", "address": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359", "decimals": 6},
        "wrapped": {"symbol": "WMATIC", "address": "0x0d500B1d8E8eF31E21C99d1Db9A6444d3ADf1270", "decimals": 18},
    },
    42161: {
        "name": "Arbitrum",
        "native_symbol": "ETH",
        "stable": {"symbol": "USDC", "address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
        "wrapped": {"symbol": "WETH", "address": "0x82af49447d8a07e3bd95bd0d56f35241523fbab1", "decimals": 18},
    },
    43114: {
        "name": "Avalanche",
        "native_symbol": "AVAX",
        "stable": {"symbol": "USDC", "address": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E", "decimals": 6},
        "wrapped": {"symbol": "WAVAX", "address": "0xB31f66AA3C1e785363F0875A1B74E27b85FD66c7", "decimals": 18},
    },
    8453: {
        "name": "Base",
        "native_symbol": "ETH",
        "stable": {"symbol": "USDC", "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "decimals": 6},
        "wrapped": {"symbol": "WETH", "address": "0x4200000000000000000000000000000000000006", "decimals": 18},
    },
    146: {
        "name": "Sonic",
        "native_symbol": "S",
        "stable": {"symbol": "USDC", "address": "0x29219dd400f2Bf60E5a23d13Be72B486D4038894", "decimals": 6},
        "wrapped": {"symbol": "wS", "address": "0x039e2fB66102314Ce7b64Ce5Ce3E5183bc94aD38", "decimals": 18},
    },
}

# =========================
# Validate DEFAULT_CHAIN_ID
# =========================
if DEFAULT_CHAIN_ID not in CHAIN_CONFIG:
    print("Available chain IDs:", sorted(CHAIN_CONFIG.keys()))
    raise SystemExit(f"Invalid CHAIN_ID in env: {DEFAULT_CHAIN_ID}")

def get_chain_id_from_request(value: str | None) -> int:
    """
    Safely parse chain_id from request. Falls back to DEFAULT_CHAIN_ID, then 1.
    """
    try:
        cid = int(value) if value is not None else DEFAULT_CHAIN_ID
    except Exception:
        cid = DEFAULT_CHAIN_ID

    if cid not in CHAIN_CONFIG:
        cid = DEFAULT_CHAIN_ID if DEFAULT_CHAIN_ID in CHAIN_CONFIG else 1
    return cid

# =========================
# Validation helpers
# =========================
ETH_ADDR_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")

def is_eth_address(s: str) -> bool:
    return bool(ETH_ADDR_RE.match((s or "").strip()))

# =========================
# APIs
# =========================
ZEROX_PRICE_URL = "https://api.0x.org/swap/allowance-holder/price"
ZEROX_HEADERS = {"0x-api-key": ZEROX_API_KEY, "0x-version": "v2"}

def covalent_tokens(chain_id: int, wallet: str):
    url = f"https://api.covalenthq.com/v1/{chain_id}/address/{wallet}/balances_v2/"
    r = session.get(
        url,
        params={"key": COVALENT_API_KEY, "no-nft-fetch": "true"},
        timeout=25,
    )
    r.raise_for_status()
    return r.json()["data"]["items"]

def zerox_price(chain_id: int, wallet: str, sell_token: str, sell_amount_raw: int, buy_token_addr: str):
    params = {
        "chainId": str(chain_id),
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

def amount_from_buy_amount(buy_amount_raw: str, buy_decimals: int) -> float:
    try:
        return int(buy_amount_raw) / (10 ** int(buy_decimals))
    except Exception:
        return 0.0

def matcha_link(chain_id: int, sell_token: str, sell_amount_raw: int, buy_token_addr: str) -> str:
    return (
        "https://matcha.xyz/swap?"
        f"chainId={chain_id}&sellToken={sell_token}&buyToken={buy_token_addr}&sellAmount={sell_amount_raw}"
    )

def thresholds_for_mode(buy_mode: str):
    """
    Returns (min_out, dust_out) depending on buy mode.
    """
    if buy_mode == "USDC":
        return MIN_USDC, DUST_MIN_USDC
    # wrapped native
    return MIN_WRAPPED_NATIVE, 0.0  # dust threshold for wrapped native not used here

def evaluate_token_fast(
    chain_id: int,
    wallet: str,
    token_addr: str,
    balance_raw: int,
    buy_mode: str,
    buy_token_addr: str,
    buy_decimals: int,
):
    """
    FAST heuristic:
      - try 1%, 0.1%, 0.01% of balance
      - if none, micro=1 to detect traps
    Returns dict with status + out_est + link/note.
    Statuses:
      - PARTIAL: can get >= min_out
      - MICRO_ONLY: only micro amounts pass threshold (often trap/maxTx/tax)
      - DUST_ONLY: route exists but below thresholds (USDC dust etc.)
      - NO_ROUTE: no route
    """
    frac_amounts = [
        max(int(balance_raw * 0.01), 1),
        max(int(balance_raw * 0.001), 1),
        max(int(balance_raw * 0.0001), 1),
    ]

    buy_mode = (buy_mode or "ETH").upper()
    if buy_mode not in ["USDC", "ETH"]:
        buy_mode = "ETH"

    min_out, dust_out = thresholds_for_mode(buy_mode)

    for amt in frac_amounts:
        q = zerox_price(chain_id, wallet, token_addr, amt, buy_token_addr)
        if not q:
            continue

        out_amt = amount_from_buy_amount(q["buyAmount"], buy_decimals)

        if out_amt >= min_out:
            return {
                "status": "PARTIAL",
                "out_est": out_amt,
                "sell_amount_raw": amt,
                "link": matcha_link(chain_id, token_addr, amt, buy_token_addr),
                "note": "",
            }

        # If in USDC mode, keep track of dust routes too
        if buy_mode == "USDC" and out_amt >= dust_out and out_amt > 0:
            return {
                "status": "DUST_ONLY",
                "out_est": out_amt,
                "sell_amount_raw": amt,
                "link": "",
                "note": "Route exists but under MIN_USDC threshold.",
            }

    # micro trap check
    q1 = zerox_price(chain_id, wallet, token_addr, 1, buy_token_addr)
    if q1:
        out1 = amount_from_buy_amount(q1["buyAmount"], buy_decimals)

        if out1 >= min_out:
            return {
                "status": "MICRO_ONLY",
                "out_est": out1,
                "sell_amount_raw": 1,
                "link": "",
                "note": "Only micro amounts hit threshold (likely trap/maxTx/tax).",
            }

        if buy_mode == "USDC" and out1 >= dust_out and out1 > 0:
            return {"status": "DUST_ONLY", "out_est": out1, "sell_amount_raw": 1, "link": "", "note": ""}

    return {"status": "NO_ROUTE", "out_est": 0.0, "sell_amount_raw": None, "link": "", "note": ""}

# =========================
# In-memory job store
# =========================
JOBS = {}
JOBS_LOCK = threading.Lock()

def push(job_id: str, event: str, payload: dict):
    with JOBS_LOCK:
        q = JOBS.get(job_id, {}).get("q")
    if not q:
        return
    payload = payload or {}
    payload["event"] = event
    q.put(payload)

def run_scan(job_id: str, wallets: list[str], buy_mode: str, chain_id: int):
    start_ts = time.time()

    cfg = CHAIN_CONFIG.get(chain_id) or CHAIN_CONFIG.get(DEFAULT_CHAIN_ID) or CHAIN_CONFIG[1]

    buy_mode = (buy_mode or "ETH").upper()
    if buy_mode not in ["USDC", "ETH"]:
        buy_mode = "ETH"

    stable = cfg["stable"]
    wrapped = cfg["wrapped"]

    buy_token = stable if buy_mode == "USDC" else wrapped
    buy_token_addr = buy_token["address"]
    buy_decimals = int(buy_token.get("decimals", 18))

    min_out, dust_out = thresholds_for_mode(buy_mode)

    # token skip list (addresses)
    skip_addrs = {stable["address"].lower(), wrapped["address"].lower()}

    push(job_id, "meta", {
        "chainId": chain_id,
        "chainName": cfg["name"],
        "wallets": wallets,
        "startedAt": start_ts,
        "buyMode": buy_mode,
        "stableSymbol": stable["symbol"],
        "wrappedSymbol": wrapped["symbol"],
        "minUsdc": MIN_USDC,
        "dustMinUsdc": DUST_MIN_USDC,
        "minWrappedNative": MIN_WRAPPED_NATIVE,
        "minOut": min_out,
        "dustOut": dust_out,
        "buyToken": buy_token["symbol"],
        "buyTokenAddr": buy_token_addr,
    })

    output = []
    errors = []

    for w_i, wallet in enumerate(wallets, start=1):
        push(job_id, "wallet_start", {"wallet": wallet, "index": w_i, "total": len(wallets)})

        try:
            items = covalent_tokens(chain_id, wallet)
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
            addr = (t.get("contract_address") or "").strip()
            if not addr:
                continue

            # Skip stable + wrapped native by address, plus obvious native symbols
            if addr.lower() in skip_addrs:
                continue
            if sym.upper() in {cfg["native_symbol"].upper(), stable["symbol"].upper(), wrapped["symbol"].upper()}:
                continue

            scanned += 1
            push(job_id, "token", {"wallet": wallet, "symbol": sym, "name": name, "contract": addr, "stage": "quoting"})

            verdict = evaluate_token_fast(
                chain_id=chain_id,
                wallet=wallet,
                token_addr=addr,
                balance_raw=bal,
                buy_mode=buy_mode,
                buy_token_addr=buy_token_addr,
                buy_decimals=buy_decimals,
            )

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

            # Always push token_result so UI updates
            push(job_id, "token_result", {
                "wallet": wallet,
                "symbol": sym,
                "name": name,
                "token": addr,
                "status": verdict["status"],
                "out_est": verdict.get("out_est", 0) or 0,
                "link": verdict.get("link", "") or "",
                "note": verdict.get("note", "") or "",
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
            JOBS[job_id]["chainId"] = chain_id

    push(job_id, "done", {"seconds": total_s, "errors": errors, "output": output, "buyMode": buy_mode, "chainId": chain_id})

# =========================
# Routes
# =========================
@app.route("/", methods=["GET"])
def home():
    chain_id = DEFAULT_CHAIN_ID if DEFAULT_CHAIN_ID in CHAIN_CONFIG else 1
    cfg = CHAIN_CONFIG[chain_id]
    return render_template(
        "index.html",
        chain_id=chain_id,
        chain_name=cfg["name"],
        chain_options=[{"id": cid, "name": CHAIN_CONFIG[cid]["name"]} for cid in sorted(CHAIN_CONFIG.keys())],
        min_usdc=MIN_USDC,
        usdc_addr=cfg["stable"]["address"],
        wrapped_addr=cfg["wrapped"]["address"],
        wrapped_symbol=cfg["wrapped"]["symbol"],
        job_id=None,
        wallets_text="",
        buy_mode="ETH",
    )

@app.route("/start", methods=["POST"])
def start():
    wallets_text = request.form.get("wallets", "")
    buy_mode = request.form.get("buy_mode", "ETH")
    chain_id = get_chain_id_from_request(request.form.get("chain_id"))

    wallets_raw = [w.strip() for w in wallets_text.splitlines() if w.strip()]
    valid = [w for w in wallets_raw if is_eth_address(w)]

    # Dedupe while preserving order
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
            "buyMode": (buy_mode or "ETH").upper(),
            "chainId": chain_id,
        }

    t = threading.Thread(target=run_scan, args=(job_id, valid, buy_mode, chain_id), daemon=True)
    t.start()

    return redirect(url_for("job_view", job_id=job_id))

@app.route("/job/<job_id>", methods=["GET"])
def job_view(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        return redirect(url_for("home"))

    chain_id = int(job.get("chainId") or DEFAULT_CHAIN_ID)
    if chain_id not in CHAIN_CONFIG:
        chain_id = DEFAULT_CHAIN_ID if DEFAULT_CHAIN_ID in CHAIN_CONFIG else 1
    cfg = CHAIN_CONFIG[chain_id]

    return render_template(
        "index.html",
        chain_id=chain_id,
        chain_name=cfg["name"],
        chain_options=[{"id": cid, "name": CHAIN_CONFIG[cid]["name"]} for cid in sorted(CHAIN_CONFIG.keys())],
        min_usdc=MIN_USDC,
        usdc_addr=cfg["stable"]["address"],
        wrapped_addr=cfg["wrapped"]["address"],
        wrapped_symbol=cfg["wrapped"]["symbol"],
        job_id=job_id,
        wallets_text=job.get("wallets_text", ""),
        buy_mode=(job.get("buyMode") or "ETH").upper(),
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
                # Keep-alive so proxies don't close the connection
                yield "event: ping\ndata: {}\n\n"

            with JOBS_LOCK:
                done = JOBS.get(job_id, {}).get("done", False)
            if done:
                time.sleep(0.2)
                return

    return Response(stream(), mimetype="text/event-stream")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
