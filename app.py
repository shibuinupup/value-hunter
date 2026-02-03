
# (shortened header for clarity)
import os, re, time, uuid, json, queue, threading, requests
from flask import Flask, render_template, request, Response, redirect, url_for
from dotenv import load_dotenv
load_dotenv()
app = Flask(__name__)

COVALENT_API_KEY=os.getenv("COVALENT_API_KEY")
ZEROX_API_KEY=os.getenv("ZEROX_API_KEY")
DEFAULT_CHAIN_ID=int(os.getenv("CHAIN_ID","1"))
MIN_USDC=float(os.getenv("MIN_USDC","0.5"))
DUST_MIN_USDC=float(os.getenv("DUST_MIN_USDC","0.01"))
MIN_WRAPPED_NATIVE=float(os.getenv("MIN_WRAPPED_NATIVE","0.0002"))
WALLET_THROTTLE_S=float(os.getenv("WALLET_THROTTLE_S","0.15"))
TOKEN_THROTTLE_S=float(os.getenv("TOKEN_THROTTLE_S","0.04"))

session=requests.Session()

CHAIN_CONFIG={1:{"name":"Ethereum","native_symbol":"ETH",
"stable":{"symbol":"USDC","address":"0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48","decimals":6},
"wrapped":{"symbol":"WETH","address":"0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2","decimals":18}}}

ZEROX_PRICE_URL="https://api.0x.org/swap/allowance-holder/price"
ZEROX_HEADERS={"0x-api-key":ZEROX_API_KEY,"0x-version":"v2"}

def zerox_price(chain_id,taker,sell_token,sell_amount_raw,buy_token_addr):
    params={"chainId":str(chain_id),"sellToken":sell_token,"buyToken":buy_token_addr,"sellAmount":str(sell_amount_raw)}
    if taker and taker!="0x0000000000000000000000000000000000000000":
        params["taker"]=taker
    r=session.get(ZEROX_PRICE_URL,params=params,headers=ZEROX_HEADERS,timeout=18)
    if r.status_code==404: return None
    if r.status_code!=200:
        print("0x error",r.status_code,r.text[:200]); return None
    j=r.json(); buy=j.get("buyAmount")
    if not buy or buy=="0": return None
    return j

@app.route("/debug_quote")
def debug_quote():
    weth=CHAIN_CONFIG[1]["wrapped"]
    usdc=CHAIN_CONFIG[1]["stable"]
    sell_amount=10**15
    q=zerox_price(1,None,weth["address"],sell_amount,usdc["address"])
    return (q or {"error":"no_route"})

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT","8000")))
