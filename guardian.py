"""
Guardián TAO Lineal (v1.0, 4-oct-2026)
======================================
Proceso SIN ESTADO que hace cumplir la gestión de v5.x/v6 aunque el bot de Render
se reinicie, se caiga o pierda su estado.json. La única fuente de verdad es Bitunix:
  - hora de apertura  -> ctime de la posición
  - entrada           -> avgOpenPrice
  - stop vigente      -> orden TP/SL de la posición
  - candado 12h hecho -> el stop ya está del lado de la ganancia (o en entrada)

Reglas (en cada corrida, idempotentes):
  1. Sin stop en Bitunix           -> pone stop de emergencia (4%) y avisa 🚨
  2. Riesgo abierto > RIESGO_MAX % -> avisa 🚨 (detecta entradas dobles)
  3. Órdenes de entrada del bot duplicadas, o pendientes con posición abierta -> las cancela
  4. Edad >= 12h y candado no hecho: si pierde -> cierra; si gana -> stop asegura 90%
  5. Edad >= 24h                    -> cierra
  6. (opcional) bot sin latido en ntfy por más de LATIDO_MIN -> avisa 🚨

Variables de entorno:
  BITUNIX_API_KEY, BITUNIX_SECRET_KEY   (llave SIN permiso de retiro)
  SIMBOLOS=TAOUSDT   GRACIA_MIN=10   RIESGO_MAX=2.0   MODO=real|papel
  NTFY_TOPIC=<canal>   NTFY_SERVER=https://ntfy.sh   LATIDO_TOPIC=<canal_estado>  LATIDO_MIN=0
  PREFIJO_BOT=taol
"""
import os, time, json, uuid, hashlib, requests

BASE = "https://fapi.bitunix.com"
H = 3600_000
CANDADO_H, HOLD_H, CANDADO = 12, 24, 0.90
STOP_EMERGENCIA = 0.04

def env(k, d=None):
    v = os.environ.get(k)
    return v if v not in (None, "") else d

# ───────── Cliente Bitunix (firma doble SHA256 de la doc oficial) ─────────
class Bitunix:
    def __init__(self, key, secret, http=None):
        self.key, self.secret, self.http = key, secret, http or requests

    def _firma(self, nonce, ts, query, body):
        q = "".join(f"{k}{query[k]}" for k in sorted(query)) if query else ""
        d = hashlib.sha256((nonce + ts + self.key + q + body).encode()).hexdigest()
        return hashlib.sha256((d + self.secret).encode()).hexdigest()

    def _req(self, metodo, path, query=None, data=None):
        nonce, ts = uuid.uuid4().hex, str(int(time.time() * 1000))
        body = json.dumps(data, separators=(",", ":")) if data is not None else ""
        hdr = {"api-key": self.key, "nonce": nonce, "timestamp": ts, "language": "en-US",
               "Content-Type": "application/json", "sign": self._firma(nonce, ts, query or {}, body)}
        if metodo == "GET":
            r = self.http.get(BASE + path, params=query, headers=hdr, timeout=20)
        else:
            r = self.http.post(BASE + path, data=body, headers=hdr, timeout=20)
        j = r.json()
        if j.get("code") != 0:
            raise RuntimeError(f"Bitunix {path}: {j}")
        return j.get("data")

    # lectura
    def posiciones(self, sym):
        d = self._req("GET", "/api/v1/futures/position/get_pending_positions", {"symbol": sym})
        return d if isinstance(d, list) else (d or {}).get("positionList", [])
    def tpsl(self, sym):
        d = self._req("GET", "/api/v1/futures/tpsl/get_pending_orders", {"symbol": sym, "limit": 100})
        return d if isinstance(d, list) else (d or {}).get("orderList", [])
    def ordenes(self, sym):
        d = self._req("GET", "/api/v1/futures/trade/get_pending_orders", {"symbol": sym, "limit": 100})
        return d if isinstance(d, list) else (d or {}).get("orderList", [])
    def precio(self, sym):
        d = requests.get(BASE + "/api/v1/futures/market/tickers", params={"symbols": sym}, timeout=20).json()["data"]
        return float(d[0]["lastPrice"])
    def capital(self):
        d = self._req("GET", "/api/v1/futures/account", {"marginCoin": "USDT"})
        d = d[0] if isinstance(d, list) else d
        return float(d.get("available", 0)) + float(d.get("margin", 0)) + float(d.get("crossUnrealizedPNL", 0) or 0) \
               + float(d.get("isolationUnrealizedPNL", 0) or 0)
    # escritura
    def cerrar(self, pid):
        return self._req("POST", "/api/v1/futures/trade/flash_close_position", data={"positionId": str(pid)})
    def poner_stop(self, sym, pid, sl, existe):
        path = "/api/v1/futures/tpsl/position/modify_order" if existe else "/api/v1/futures/tpsl/position/place_order"
        return self._req("POST", path, data={"symbol": sym, "positionId": str(pid),
                                              "slPrice": f"{sl:.4f}".rstrip("0").rstrip("."), "slStopType": "LAST_PRICE"})
    def cancelar(self, sym, oids):
        return self._req("POST", "/api/v1/futures/trade/cancel_orders",
                         data={"symbol": sym, "orderList": [{"orderId": str(o)} for o in oids]})

# ───────── Lógica pura (sin red): decide qué hacer ─────────
def lado_de(pos):
    s = str(pos.get("side", "")).upper()
    return 1 if s in ("LONG", "BUY") else -1          # la doc dice LONG/SHORT; el historial usa BUY/SELL

def stop_de(pos, tpsl):
    for t in tpsl:
        if str(t.get("positionId")) == str(pos.get("positionId")) and t.get("slPrice") not in (None, "", "0"):
            return float(t["slPrice"]), True
    return None, False

def decidir(pos, tpsl, ordenes, precio, ahora, capital, cfg):
    """Devuelve lista de acciones: ("cerrar", motivo) | ("stop", precio, existe, motivo) | ("cancelar", [oids], motivo) | ("aviso", texto)"""
    acc = []
    lado = lado_de(pos)
    ent = float(pos.get("avgOpenPrice") or pos.get("entryPrice"))
    qty = float(pos.get("qty"))
    t0 = int(pos["ctime"])
    edad_h = (ahora - t0) / H
    gracia = cfg["gracia_min"] / 60
    sl, existe = stop_de(pos, tpsl)

    # 3) órdenes de entrada del bot pendientes mientras hay posición -> fuera (evita doblar la posición)
    pref = cfg["prefijo"]
    entradas = [o for o in ordenes if str(o.get("clientId") or "").startswith(pref) and not o.get("reduceOnly")]
    if entradas:
        acc.append(("cancelar", [o["orderId"] for o in entradas], "orden de entrada del bot pendiente con posición ya abierta"))

    # 5) salida 24h
    if edad_h >= HOLD_H + gracia:
        acc.append(("cerrar", f"24h cumplidas ({edad_h:.1f}h)"))
        return acc

    # 1) sin stop
    if sl is None:
        sl = ent * (1 - STOP_EMERGENCIA * lado)
        acc.append(("stop", sl, False, "🚨 posición SIN stop en Bitunix: stop de emergencia al 4%"))
        existe = True

    # 2) riesgo abierto
    riesgo = max(0.0, (ent - sl) * lado) * qty
    if capital and riesgo / capital * 100 > cfg["riesgo_max"]:
        acc.append(("aviso", f"🚨 riesgo abierto {riesgo:.2f} USDT = {riesgo/capital*100:.1f}% del capital "
                             f"(máx {cfg['riesgo_max']}%). Posible entrada doble: qty {qty}."))

    # 4) revisión 12h
    candado_hecho = (sl - ent) * lado >= 0          # stop ya en entrada o del lado de la ganancia
    if edad_h >= CANDADO_H + gracia and not candado_hecho:
        g = (precio - ent) * lado
        if g <= 0:
            acc.append(("cerrar", f"revisión 12h: va perdiendo ({g:+.2f} por unidad)"))
        else:
            nuevo = ent + lado * CANDADO * g
            if (nuevo - sl) * lado > 0:
                acc.append(("stop", nuevo, existe, f"revisión 12h: gana {g:+.2f}; stop asegura 90% en {nuevo:.2f}"))
    return acc

def depurar_entradas_sin_posicion(ordenes, cfg):
    """Sin posición: si hay más de una orden de entrada del bot viva, deja solo la más nueva."""
    pref = cfg["prefijo"]
    e = sorted([o for o in ordenes if str(o.get("clientId") or "").startswith(pref) and not o.get("reduceOnly")],
               key=lambda o: int(o.get("ctime") or 0))
    if len(e) > 1:
        return [("cancelar", [o["orderId"] for o in e[:-1]], f"{len(e)} órdenes de entrada del bot vivas a la vez (entrada doble)")]
    return []

# ───────── Ejecución ─────────
def avisar(cfg, texto, prioridad=4):
    print(texto)
    if cfg.get("ntfy"):
        try:
            requests.post(f"{cfg['ntfy_server']}/{cfg['ntfy']}", data=texto.encode(),
                          headers={"Title": "Guardián TAO", "Priority": str(prioridad)}, timeout=15)
        except Exception as e:
            print("ntfy:", e)

def latido_ok(cfg):
    if not cfg["latido_topic"] or cfg["latido_min"] <= 0:
        return True
    r = requests.get(f"{cfg['ntfy_server']}/{cfg['latido_topic']}/json",
                     params={"poll": "1", "since": f"{cfg['latido_min']}m"}, timeout=20)
    return any(l.strip() for l in r.text.splitlines())

def correr(bx, cfg, ahora=None, precio_fn=None, capital_fn=None):
    ahora = ahora or int(time.time() * 1000)
    hechos = []
    for sym in cfg["simbolos"]:
        posiciones, tpsl, ordenes = bx.posiciones(sym), bx.tpsl(sym), bx.ordenes(sym)
        if not posiciones:
            acciones = [(sym, None, a) for a in depurar_entradas_sin_posicion(ordenes, cfg)]
        else:
            precio = (precio_fn or bx.precio)(sym)
            cap = (capital_fn or bx.capital)()
            acciones = [(sym, p, a) for p in posiciones for a in decidir(p, tpsl, ordenes, precio, ahora, cap, cfg)]
        for sym_, pos, a in acciones:
            txt = f"🛡️ {sym_}: " + (a[-1] if a[0] != "aviso" else a[1])
            if cfg["modo"] != "real" and a[0] != "aviso":
                txt += "  [MODO PAPEL: no ejecutado]"
            else:
                try:
                    if a[0] == "cerrar":
                        bx.cerrar(pos["positionId"])
                    elif a[0] == "stop":
                        bx.poner_stop(sym_, pos["positionId"], a[1], a[2])
                    elif a[0] == "cancelar":
                        bx.cancelar(sym_, a[1])
                except Exception as e:
                    txt = f"🚨 {sym_}: FALLÓ '{a[-1]}': {e}. Hazlo a mano."
            avisar(cfg, txt, 5 if "🚨" in txt else 4)
            hechos.append((a[0], txt))
    if not latido_ok(cfg):
        avisar(cfg, f"🚨 El bot no publica estado hace más de {cfg['latido_min']} min (¿Render caído?).", 5)
        hechos.append(("aviso", "sin latido"))
    return hechos

def config():
    return {"simbolos": env("SIMBOLOS", "TAOUSDT").split(","), "gracia_min": float(env("GRACIA_MIN", 10)),
            "riesgo_max": float(env("RIESGO_MAX", 2.0)), "modo": env("MODO", "papel"), "prefijo": env("PREFIJO_BOT", "taol"),
            "ntfy": env("NTFY_TOPIC"), "ntfy_server": env("NTFY_SERVER", "https://ntfy.sh"),
            "latido_topic": env("LATIDO_TOPIC"), "latido_min": float(env("LATIDO_MIN", 0))}

if __name__ == "__main__":
    cfg = config()
    bx = Bitunix(env("BITUNIX_API_KEY"), env("BITUNIX_SECRET_KEY"))
    r = correr(bx, cfg)
    print(f"Guardián: {len(r)} acciones · modo {cfg['modo']}")
